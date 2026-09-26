"""Reconstruct damage from observed state and build-specific, inspected formulas.

Totals always come from damage broadcasts. Component credit uses a waterfall:
base, source scaling, additive outgoing bonuses, then later multipliers. A hit
enters component totals only when the entire reconstruction equals the log.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from math import isqrt
from struct import pack, unpack

from .parser import Event


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(files("inkbound_meter").joinpath("damage_catalog.json").read_text("utf-8"))


def trunc_div(n: int, d: int) -> int:
    return (1 if n * d >= 0 else -1) * (abs(n) // abs(d))


def fix_ratio(n: int, d: int = 100) -> int:
    return (trunc_div(n << 13, d) + 1) >> 1


def fix_round(raw: int) -> int:
    return (raw + 2048) >> 12


def fix_to_int(raw: int) -> int:
    """FixedPointyExtensions.ToInt uses Math.Round on a single-precision float."""
    return round(unpack("<f", pack("<f", raw / 4096))[0])


def apply_multiplier(value: int, remainder: int, multiplier: int) -> tuple[int, int]:
    """The game's hundredths carry, including truncation at each multiplier."""
    scaled = value * multiplier
    carry = trunc_div(remainder * multiplier, 100) + scaled % 100
    return scaled // 100 + carry // 100, carry % 100


@dataclass
class Breakdown:
    hits: int = 0
    matched_hits: int = 0
    matched_damage: int = 0
    unresolved_damage: int = 0
    # Integer hundredths prevent drift across long runs.
    credits: Counter = field(default_factory=Counter)
    order: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    latest: dict = field(default_factory=dict)

    def add(self, result: dict, damage: int) -> None:
        self.hits += 1
        self.latest = result
        self._credit(result, damage)

    def _credit(self, result: dict, damage: int) -> None:
        if result["status"] == "matched":
            self.matched_hits += 1
            self.matched_damage += damage
            self.credits.update(result["credits"])
            for name in result["order"]:
                if name not in self.order:
                    self.order.append(name)
        else:
            self.unresolved_damage += damage
            self.reasons[result["reason"]] += 1

    def resolve(self, previous: dict, result: dict, damage: int) -> None:
        """Replace an unresolved explanation when later evidence confirms it."""
        assert previous["status"] != "matched" and result["status"] == "matched"
        self.unresolved_damage -= damage
        reason = previous["reason"]
        self.reasons[reason] -= 1
        if not self.reasons[reason]:
            del self.reasons[reason]
        self._credit(result, damage)
        if self.latest is previous:
            self.latest = result

    def view(self) -> dict:
        return {
            "hits": self.hits,
            "matched_hits": self.matched_hits,
            "matched_damage": self.matched_damage,
            "unresolved_hits": self.hits - self.matched_hits,
            "unresolved_damage": self.unresolved_damage,
            "components": [
                {"name": k, "damage": v / 100}
                for k, v in sorted(
                    self.credits.items(),
                    key=lambda item: (item[0] == "Rounding", self.order.index(item[0])),
                )
                if v
            ],
            "reasons": dict(self.reasons),
            "latest": {k: v for k, v in self.latest.items() if k not in ("credits", "order")},
        }


class DamageContext:
    def __init__(self):
        self.build: int | None = None
        self.full_history = False
        self.pending_run = False
        self.stats: dict[int, dict[str, int]] = defaultdict(dict)
        self.units: dict[int, str] = {}
        self.fresh: set[int] = set()
        self.statuses: dict[tuple[int, int], dict] = {}
        self.positions: dict[int, list[int]] = {}
        self.hitbox_radii: dict[int, int] = {}
        self.cast_abilities: dict[int, str] = {}
        self.cast_timestamps: dict[int, str] = {}
        self.damage_health: dict | None = None

    def clear_session_state(self) -> None:
        """Discard observed combat state while retaining the mapped game build."""
        self.stats.clear()
        self.statuses.clear()
        self.units.clear()
        self.fresh.clear()
        self.positions.clear()
        self.hitbox_radii.clear()
        self.cast_abilities.clear()
        self.cast_timestamps.clear()
        self.damage_health = None
        self.full_history = False

    def apply(self, event: Event) -> None:
        d = event.data

        def observe_shield(entity, value):
            # Current Energy Shield is a live resource rather than a permanent
            # stat. The game supplies it with every authoritative health/shield
            # state, so retain that value for shield-scaling damage formulas.
            self.stats[entity]["A3xbQ1as"] = value

        if event.kind == "source_started":
            self.__init__()
        elif event.kind == "game_build":
            self.build = d["build"]
        elif event.kind == "run_create":
            # The game can announce resumed peers immediately after this event
            # but before its connection confirmation. Start a new snapshot now
            # so those authoritative player records survive that confirmation.
            self.clear_session_state()
            self.pending_run = True
        elif event.kind == "connection":
            if d["connected"]:
                if not self.pending_run:
                    self.clear_session_state()
                self.full_history = self.pending_run
                self.pending_run = False
            else:
                self.full_history = False
        elif event.kind in ("unit", "player", "class"):
            entity = d["id"]
            if "shield" in d:
                observe_shield(entity, d["shield"])
            if "position" in d:
                self.positions[entity] = d["position"]
            if "hitbox_radius" in d:
                self.hitbox_radii[entity] = d["hitbox_radius"]
            unit = d.get("unit_data")
            if unit and entity not in self.units:
                self.units[entity] = unit
                if unit in catalog()["units"]:
                    # Party peers are commonly announced as resuming even when
                    # this log contains the run start. Their immutable unit and
                    # class baselines are still known from the inspected assets;
                    # later stat broadcasts replace those defaults. Exact hit
                    # matching remains the guard against unseen loadout state.
                    self.fresh.add(entity)
                    for k, v in catalog()["units"][unit].items():
                        self.stats[entity].setdefault(k, v)
            if d.get("class_id") in catalog()["classes"] and entity in self.fresh:
                for k, v in catalog()["classes"][d["class_id"]].items():
                    self.stats[entity].setdefault(k, v)
        elif event.kind == "stat":
            self.stats[d["id"]][d["stat"]] = d["value"]
        elif event.kind in ("health", "heal", "shield"):
            observe_shield(d["id"] if event.kind == "health" else d["target"], d["shield"])
        elif event.kind == "damage_health":
            # This local diagnostic immediately precedes the authoritative
            # damage broadcast. It supplies the post-hit HP needed for the
            # explicit 1-HP near-death cap; it never adds damage by itself.
            self.damage_health = d
        elif event.kind == "status":
            key = (d["target"], d["instance"])
            if d["stacks"] > 0:
                self.statuses[key] = d
            else:
                self.statuses.pop(key, None)
        elif event.kind == "position":
            self.positions[d["id"]] = d["position"]
            if "ability" in d:
                self.cast_abilities[d["id"]] = d["ability"]
                self.cast_timestamps[d["id"]] = event.timestamp
        elif event.kind in ("combat_start", "combat_end", "turn_phase"):
            self.cast_abilities.clear()
            self.cast_timestamps.clear()
        elif event.kind == "unit_removed":
            self.positions.pop(d["id"], None)
            self.hitbox_radii.pop(d["id"], None)
            self.cast_abilities.pop(d["id"], None)

    def stacks(self, entity: int, effect: str, source: int | None = None) -> int:
        return sum(
            v["stacks"]
            for (target, _), v in self.statuses.items()
            if target == entity
            and v["effect"] == effect
            and (source is None or source == v["source"])
        )

    def explain(
        self,
        event: Event,
        *,
        incoming_spec: dict | None = None,
        direct_multiplier: int | None = None,
    ) -> dict:
        d = event.data
        result = {
            "status": "unavailable",
            "observed": d["amount"],
            "timestamp": event.timestamp,
            "target": d["target"],
        }

        def unavailable(reason):
            return {**result, "reason": reason}

        cat = catalog()
        if self.build != cat["build"]:
            return unavailable("Game build is missing or not mapped")
        spec = incoming_spec or cat["actions"].get(d.get("action"))
        if not spec:
            return unavailable("This source's formula is not mapped yet")
        if not self.full_history or d["source"] not in self.fresh:
            return unavailable("Initial player stats are missing")
        if d["target"] not in self.fresh:
            return unavailable("Initial target stats are missing")
        if d["amount"] == 0 and incoming_spec is None:
            return unavailable("Zero hit: immunity or phase state may apply")
        ability = cat["abilities"].get(d.get("ability"))
        if d.get("ability") and ability is None:
            return unavailable("This ability's modifiers are not mapped yet")
        if ability and ability["range_mode"] in (4, 11) and direct_multiplier is None:
            return unavailable("Direct-target difficulty scaling is not mapped")

        entities = {"source": d["source"], "target": d["target"]}
        inputs = {}
        distance = None
        if '"distance_floor"' in json.dumps(spec["amount"]):
            if (
                not d.get("ability")
                or self.cast_abilities.get(d["source"]) != d["ability"]
                or any(entity not in self.positions for entity in entities.values())
            ):
                return unavailable("Positions for this cast are missing")
            a, b = (self.positions[entities[side]] for side in ("source", "target"))
            squared = sum((x - y) ** 2 for x, y in zip(a, b, strict=True))
            distance = ((isqrt(squared << 2) + 1) >> 1) // 4096

        distance_raw = None
        distance_multiplier = None
        distance_ring = None
        if ability and ability["distance"]:
            has_distance_evidence = (
                d.get("ability")
                and all(entity in self.positions for entity in entities.values())
                and d["target"] in self.hitbox_radii
                # Triggered effects can replace the cast record while retaining
                # the ability's source position during the same game tick. The
                # damage broadcast remains authoritative for the ability that
                # owns range scaling; do not carry that exception into a later
                # event, where positions could be stale.
                and (
                    self.cast_abilities.get(d["source"]) == d["ability"]
                    or self.cast_timestamps.get(d["source"]) == event.timestamp
                )
            )
            if has_distance_evidence:
                scaling = ability["distance_scaling"]
                if not scaling or scaling["segments"] <= 1:
                    return unavailable("This ability's distance scaling is not mapped")
                a, b = (self.positions[entities[side]] for side in ("source", "target"))
                squared = sum((x - y) ** 2 for x, y in zip(a, b, strict=True))
                distance_raw = (isqrt(squared << 2) + 1) >> 1
                decreasing = scaling["max_multiplier_raw"] < scaling["min_multiplier_raw"]
                edge_distance = distance_raw + (
                    self.hitbox_radii[d["target"]]
                    if decreasing
                    else -self.hitbox_radii[d["target"]]
                )
                if scaling["custom_segments"] is None:
                    width = scaling["width_raw"] // scaling["segments"]
                    distance_ring = max(
                        scaling["segments"] - ((edge_distance + width - 1) // width), 0
                    )
                else:
                    distance_ring = scaling["segments"] - 1
                    for percent in scaling["custom_segments"]:
                        if edge_distance >= scaling["width_raw"] * percent // 100:
                            distance_ring -= 1
                    distance_ring = max(distance_ring, 0)
                minimum = fix_round(scaling["min_multiplier_raw"] * 100)
                maximum = fix_round(scaling["max_multiplier_raw"] * 100)
                step = abs(maximum - minimum) // (scaling["segments"] - 1)
                distance_multiplier = minimum + step * distance_ring * (
                    1 if maximum >= minimum else -1
                )

        def stat(side, key):
            value = self.stats[entities[side]].get(key, 0)
            name = cat["stats"].get(key, key)
            inputs[("Target: " if side == "target" else "") + name] = value
            return value

        def evaluate(expr, active=None):
            if isinstance(expr, int):
                return expr
            op = expr["op"]
            if op == "scaled_stat" and incoming_spec is not None:
                # CalcAmount divides in fixed-point before multiplying the roll.
                return fix_to_int(fix_ratio(stat("source", expr["id"])) * expr["percent"])
            if op in ("stat", "stacks", "when", "distance_floor"):
                key = json.dumps(expr, sort_keys=True)
                if active is not None and key not in active:
                    return 0
                if op == "distance_floor":
                    inputs["Distance (whole units)"] = distance
                    return distance
                if op == "stat":
                    return stat(expr["side"], expr["id"])
                if op == "stacks":
                    value = self.stacks(entities[expr["side"]], expr["id"])
                    inputs[cat["statuses"].get(expr["id"], expr["id"])] = value
                    return value
                side = expr.get("side", "source")
                source = entities[expr["source"]] if expr.get("source") else None
                count = self.stacks(entities[side], expr["effect"], source)
                inputs[cat["statuses"].get(expr["effect"], expr["effect"])] = count
                if not count:
                    return 0
                return evaluate(expr["args"][0])
            args = [evaluate(a, active) for a in expr["args"]]
            if op == "add":
                return sum(args)
            if op == "mul":
                return args[0] * args[1]
            if op == "div":
                if not args[1]:
                    # During waterfall attribution a variable starts disabled.
                    # A denominator driven by that same variable therefore
                    # contributes no base damage until it is enabled.
                    return 0
                return trunc_div(args[0], args[1])
            if op == "div100":
                return trunc_div(args[0], 100)
            raise ValueError(f"Unknown formula operation {op}")

        # Credit source-specific scaling in expression order. Multiplicative
        # interactions are credited to the later input, never counted twice.
        terms = []

        def variables(expr):
            if isinstance(expr, int):
                return
            if expr["op"] == "scaled_stat":
                return
            if expr["op"] in ("stat", "stacks", "when", "distance_floor"):
                if expr not in terms:
                    terms.append(expr)
            else:
                for arg in expr["args"]:
                    variables(arg)

        variables(spec["amount"])
        active: set[str] = set()
        base = evaluate(spec["amount"], active)
        credits = Counter({"Base effect": base * 100})
        for term in terms:
            active.add(json.dumps(term, sort_keys=True))
            after = evaluate(spec["amount"], active)
            name = term.get("name") or cat["stats" if term["op"] == "stat" else "statuses"].get(
                term.get("id"), term.get("id")
            )
            if term.get("side") == "target":
                name = "Target: " + name
            credits[name] += (after - base) * 100
            base = after
        if base < 0:
            return unavailable("Negative base damage is unsupported")
        value, remainder = base, 0

        outgoing = [("General damage", max(0, stat("source", "pvw9uUnH")))]
        for entry in cat["statTagEntries"]:
            if entry["tag"] in spec["tags"]:
                bonus = max(0, stat("source", entry["stat"]))
                percent = bonus * fix_round(entry["multiplier_raw"] * 100)
                outgoing.append((cat["stats"][entry["stat"]], percent))
        for name, percent in outgoing:
            credits[name] += base * percent
        value, remainder = apply_multiplier(value, remainder, 100 + sum(p for _, p in outgoing))
        stages = []

        def multiply(name, percent):
            nonlocal value, remainder
            before = value * 100 + remainder
            value, remainder = apply_multiplier(value, remainder, percent)
            delta = value * 100 + remainder - before
            credits[name] += delta
            if percent != 100:
                stages.append({"name": name, "multiplier": percent / 100})

        def multiply_group(parts):
            # The game combines these modifiers before applying the multiplier.
            # Compare successive combined values against the same input to split
            # credit without introducing extra rounding between modifiers.
            nonlocal value, remainder
            start_value, start_remainder = value, remainder
            for name, percent in parts:
                before = value * 100 + remainder
                value, remainder = apply_multiplier(start_value, start_remainder, percent)
                delta = value * 100 + remainder - before
                if delta:
                    credits[name] += delta
                    stages.append({"name": name, "combined_multiplier": percent / 100})

        if spec["direct"]:
            binding = 100
            parts = []
            for entry in cat["bindingOnlyMultiplierStats"]:
                bonus = max(0, stat("source", entry["stat"]))
                binding = fix_round(binding * (4096 + bonus * entry["multiplier_raw"]))
                parts.append((cat["stats"][entry["stat"]], binding))
            multiply_group(parts)
            if direct_multiplier is not None:
                multiply("Direct attack scaling", direct_multiplier)
            incoming = 100
            parts = []
            for entry in cat["status_bonuses"] + (ability or {}).get("status_bonuses", []):
                bonus = max(0, stat("source", entry["stat"]))
                if bonus and self.stacks(d["target"], entry["effect"]):
                    incoming = fix_round(incoming * (4096 + fix_ratio(bonus)))
                    parts.append((cat["stats"][entry["stat"]], incoming))
            incoming = fix_round(incoming * fix_ratio(100 + max(0, stat("target", "FtBty9sm"))))
            parts.append(("Target vulnerability", incoming))
            weakness = 100
            for entry in cat["weaknessTagEntries"]:
                if entry["tag"] in spec["tags"]:
                    bonus = max(0, stat("target", entry["stat"]))
                    if bonus:
                        weakness = fix_round(
                            trunc_div(
                                weakness * (4096 + fix_ratio(bonus)) * entry["multiplier_raw"], 4096
                            )
                        )
            incoming = fix_round(fix_ratio(incoming * weakness))
            parts.append(("Target weakness", incoming))
            multiply_group(parts)
            if d["critical"]:
                bonus = max(0, stat("source", "3FXVvW6T") + evaluate(spec["crit"]))
                multiply("Critical hit", 100 + bonus)
            if distance_multiplier is not None:
                inputs["Scaled range ring"] = distance_ring
                inputs["Distance to target edge"] = distance_raw / 4096
                multiply("Scaled range", distance_multiplier)
        elif d["critical"]:
            return unavailable("Unexpected critical hit for an indirect source")

        # Increase stages round up, reductions round down, then DoT stacks multiply.
        if remainder:
            credits["Rounding"] += 100 - remainder
            value += 1
            remainder = 0
        before_defenses = value
        prevention = []

        def reduce_damage(key, caption, percent):
            before = value
            multiply(caption, percent)
            prevention.append({"stat": key, "name": caption, "amount": before - value})

        for key, caption in [
            ("3TCx0XPg", "Damage reduction"),
            ("pq6hlsOe", "Uncapped damage reduction"),
        ]:
            reduce_damage(key, caption, 100 - min(100, max(0, stat("target", key))))
        resist = sum(
            max(0, stat("target", e["stat"]))
            for e in cat["resistanceTagEntries"]
            if e["tag"] in spec["tags"]
        )
        if resist > 100:
            return unavailable("Resistance exceeds the supported range")
        reduce_damage(None, "Resistance", 100 - resist)
        outgoing_reduction = max(0, stat("source", "0QnywPkl"))
        if outgoing_reduction > 100:
            return unavailable("Outgoing reduction exceeds the supported range")
        reduce_damage("0QnywPkl", "Outgoing reduction", 100 - outgoing_reduction)
        credits["Rounding"] -= remainder
        if spec["direct"]:
            after = max(0, value - stat("target", "TWM45OpA"))
            credits["Flat damage reduction"] += (after - value) * 100
            prevention.append(
                {"stat": "TWM45OpA", "name": "Flat damage reduction", "amount": value - after}
            )
            value = after
        stacks = 1
        if isinstance(spec["stacks"], dict) and "effect" in spec["stacks"]:
            stack_entity = entities[spec["stacks"].get("side", "target")]
            instances = [
                v
                for (target, _), v in self.statuses.items()
                if target == stack_entity
                and (not spec["stacks"].get("caster_filter", True) or v["source"] == d["source"])
                and v["effect"] == spec["stacks"]["effect"]
            ]
            if len(instances) != 1:
                return unavailable("Damage-over-time stack instance is missing or ambiguous")
            stacks = instances[0]["stacks"]
        elif spec["stacks"]:
            stacks = max(1, evaluate(spec["stacks"]))
        value *= stacks
        if incoming_spec is not None:
            result["before_defenses"] = before_defenses * stacks
            result["prevention"] = [{**p, "amount": p["amount"] * stacks} for p in prevention]
        health = self.damage_health
        # Client logs expose the helper status on standard near-death enemies;
        # the original gameplay status is retained for older captures.
        near_death = self.stacks(d["target"], "CanBeNearDeath_StatusEffect") or self.stacks(
            d["target"], "CanBeNearDeath_AddHelperStatus_StatusEffect"
        )
        if (
            near_death
            and health
            and health["target"] == d["target"]
            and health["source"] == d["source"]
            and health["amount"] == d["amount"]
            and health["hp"] == 1
            and d["amount"] < value
        ):
            credits["Near-death limit"] += (d["amount"] - value) * 100
            value = d["amount"]
            inputs["Near-death limit"] = 1
            stages.append({"name": "Near-death limit", "hp_floor": 1})
        order = list(credits)
        credits = {k: v * stacks for k, v in credits.items() if v}
        assert sum(credits.values()) == value * 100
        result.update(
            predicted=value,
            inputs=inputs,
            stages=stages,
            stacks=stacks,
            components=[{"name": k, "damage": v / 100} for k, v in credits.items()],
        )
        if value != d["amount"]:
            result.update(
                status="mismatch",
                reason="Formula differs from logged hit",
                difference=d["amount"] - value,
            )
        else:
            result.update(status="matched", credits=credits, order=order)
        return result
