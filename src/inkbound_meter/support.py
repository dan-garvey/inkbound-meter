"""Incoming damage and provider-attributed support, with explicit unknowns.

Health diagnostics annotate broadcasts; they never count as extra hits. Shield
grants retain their provider while consumed oldest-first, an accounting convention
for the game's shared shield pool, not a claim about hidden shield ownership.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files

from .components import DamageContext, fix_ratio, fix_to_int, trunc_div
from .mitigation import MitigationContext
from .parser import Event

METRICS = {
    "taken": ("Taken", "Damage taken", "Logged incoming damage, including shields and overkill."),
    "shielding": (
        "Shielded",
        "Shields provided",
        "All shield granted, including unused shield. Shared shields are spent oldest-first.",
    ),
    "healing": (
        "Healed",
        "Healing provided",
        "HP restored plus reconstructed overhealing. Unknown overheal is not assumed to be zero.",
    ),
    "pressure": (
        "Enemy hits",
        "Enemy hits received",
        "Enemy attack hit events, including dodges; excludes ongoing effects. "
        "A pressure proxy, not targeting time or threat. Area attacks can hit several players.",
    ),
}


@lru_cache(maxsize=1)
def support_catalog():
    return json.loads(files("inkbound_meter").joinpath("support_catalog.json").read_text("utf-8"))


def log_time(timestamp):
    """Seconds from LogProviderFile's day offset and UTC clock.

    The last two digits are frameCount % 100, not fractional seconds. They
    routinely wrap backwards within a second and must not expire a valid cast.
    """
    match = re.fullmatch(r"(\d+)T(\d{2}):(\d{2}):(\d{2}) (\d{2})", timestamp)
    if not match:
        return None
    day, hour, minute, second, _ = map(int, match.groups())
    return ((day * 24 + hour) * 60 + minute) * 60 + second


def entry(entity, source, amount, components, *, target=None, unknown_overheal=False):
    return {
        "entity": entity,
        "source": source,
        "amount": amount,
        "components": Counter(components),
        "target": target,
        "unknown_overheal": unknown_overheal,
    }


@dataclass
class SupportTotals:
    entries: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))

    def add(self, metric, value):
        self.entries[metric].append(value)

    def view(self, players, *, rounds=0, will_spent=None):
        from .model import CLASS_NAMES, source_label

        will_spent = will_spent or Counter()
        views = {}
        for metric in METRICS:
            values = self.entries.get(metric, [])
            total = sum(v["amount"] for v in values if v["entity"] in players)
            rows = []
            for entity, player in players.items():
                owned = [v for v in values if v["entity"] == entity]
                amount = sum(v["amount"] for v in owned)
                components = Counter()
                sources = Counter()
                recipients = Counter()
                for value in owned:
                    components.update(value["components"])
                    sources[value["source"]] += value["amount"]
                    if value["target"] is not None:
                        recipients[value["target"]] += value["amount"]
                rows.append(
                    {
                        "id": entity,
                        "name": player.name or f"Player {entity}",
                        "class": CLASS_NAMES.get(player.class_id, player.class_id),
                        "amount": amount,
                        "share": amount / total if total else 0,
                        "per_round": amount / rounds if rounds else None,
                        "rounds": rounds,
                        "will_spent": will_spent[entity],
                        "per_will": amount / will_spent[entity]
                        if metric in ("shielding", "healing") and will_spent[entity]
                        else None,
                        "components": [
                            {"name": k, "damage": v} for k, v in components.items() if v
                        ],
                        "events": len(owned),
                        "unknown_overheal": sum(v["unknown_overheal"] for v in owned),
                        "sources": [
                            {"name": source_label(k), "amount": v} for k, v in sources.most_common()
                        ],
                        "recipients": [
                            {"name": players[k].name or f"Player {k}", "amount": v}
                            for k, v in recipients.most_common()
                            if k in players
                        ],
                    }
                )
                if metric == "taken":
                    credits, reasons, providers = Counter(), Counter(), Counter()
                    matched = 0
                    for value in owned:
                        mitigation = value.get("mitigation", {})
                        credits.update(mitigation.get("credits", {}))
                        if mitigation.get("status") == "matched":
                            matched += 1
                        else:
                            reasons[
                                mitigation.get("reason", "Initial combat stats are missing")
                            ] += 1
                        providers.update(value.get("shield_providers", {}))
                    rows[-1]["mitigation"] = {
                        "prevented": sum(credits.values()) / 100,
                        "absorbed": components["Absorbed"],
                        "components": [
                            {"name": k, "damage": v / 100} for k, v in credits.items() if v
                        ],
                        "matched_hits": matched,
                        "unresolved_hits": len(owned) - matched,
                        "reasons": dict(reasons),
                        "shield_providers": [
                            {
                                "name": players[k].name or f"Player {k}"
                                if k in players
                                else "Unknown provider",
                                "amount": v,
                            }
                            for k, v in providers.most_common()
                            if v
                        ],
                    }
            rows.sort(key=lambda row: (-row["amount"], row["id"]))
            unassigned = [v for v in values if v["entity"] not in players]
            views[metric] = {
                "party_total": total,
                "players": rows,
                "unattributed": sum(v["amount"] for v in unassigned),
                "unattributed_events": len(unassigned),
                "unknown_overheal": sum(v["unknown_overheal"] for v in values),
                "rounds": rounds,
                "will_spent": sum(will_spent[entity] for entity in players),
                "per_round": total / rounds if rounds else None,
                "per_will": total / sum(will_spent[entity] for entity in players)
                if metric in ("shielding", "healing")
                and sum(will_spent[entity] for entity in players)
                else None,
            }
        return views


class SupportContext:
    def __init__(self):
        self.health = {}
        self.pools = defaultdict(deque)
        self.damage_health = None
        self.providers = {}
        self.receipts = {}
        self.classes = {}
        self.mitigation = MitigationContext()
        self.heal_casts = {}

    def _observe_heal_cast(self, event):
        """Keep bounded, exact-ability candidates; ambiguous casters stay unknown."""
        d = event.data
        if event.kind in (
            "run_create",
            "run_end",
            "combat_start",
            "combat_end",
            "turn_phase",
            "parse_error",
        ):
            self.heal_casts.clear()
        if event.kind == "unit_removed":
            self.heal_casts = {k: v for k, v in self.heal_casts.items() if k[0] != d["id"]}
        now = log_time(event.timestamp)
        if now is not None:
            self.heal_casts = {
                k: v for k, v in self.heal_casts.items() if 0 <= now - v["time"] <= 2
            }
        if event.kind == "position" and "ability" in d:
            if now is not None and d["ability"] in support_catalog()["abilities"]:
                self.heal_casts[(d["id"], d["ability"])] = {
                    "time": now,
                    "used": Counter(),
                    "ambiguous": set(),
                }

    def _cast_provider(self, event, provider=None):
        d = event.data
        ability, action, target = d.get("ability"), d.get("action"), d["target"]
        limit = support_catalog()["abilities"].get(ability, {}).get(action, 0)
        now = log_time(event.timestamp)
        candidates = {
            owner: cast
            for (owner, played), cast in self.heal_casts.items()
            if played == ability
            and now is not None
            and 0 <= now - cast["time"] <= 2
            and cast["used"][(action, target)] < limit
            and (provider is None or owner == provider)
        }
        if len(candidates) > 1:
            for cast in candidates.values():
                cast["ambiguous"].add((action, target))
        elif len(candidates) == 1:
            owner, cast = next(iter(candidates.items()))
            if provider is None and (action, target) in cast["ambiguous"]:
                return None
            cast["used"][(action, target)] += 1
            return owner
        return None

    def _drain(self, target, amount, category):
        pool = self.pools[target]
        providers = Counter()
        while amount > 0 and pool:
            grant = pool[0]
            used = min(amount, grant["components"]["Active shield"])
            grant["components"]["Active shield"] -= used
            grant["components"][category] += used
            amount -= used
            providers[grant["entity"]] += used
            if not grant["components"]["Active shield"]:
                pool.popleft()
        return providers

    def _align(self, target, shield, category="Unresolved"):
        tracked = sum(v["components"]["Active shield"] for v in self.pools[target])
        if tracked > shield:
            self._drain(target, tracked - shield, category)
        elif tracked < shield:
            # Shields already present at capture start have no known provider.
            self.pools[target].append(
                entry(None, "unknown", shield - tracked, {"Active shield": shield - tracked})
            )

    def _provider(self, event, context, recipe):
        data = event.data
        receipt = self.receipts.pop(event.kind, None)
        self.providers.pop(event.kind, None)
        if receipt and receipt[1:] == (data["target"], event.timestamp):
            if event.kind == "heal" and recipe:
                self._cast_provider(event, receipt[0])
            return receipt[0]
        if event.kind == "heal" and recipe:
            if recipe["owner"] == recipe["target"]:
                return data["target"]
            if recipe["owner"] == "status_source" and recipe["target"] == "status_target":
                owners = {
                    s["source"]
                    for s in context.statuses.values()
                    if s["target"] == data["target"] and s["effect"] == data.get("effect")
                }
                if len(owners) == 1:
                    return owners.pop()
            if recipe["owner"] == "caster" and recipe["target"] == "target":
                return self._cast_provider(event)
        return None

    def _requested_heal(self, data, source, recipe, context):
        # ApplyHeal caps to max HP. If the resulting HP is still below that cap,
        # the broadcast proves that no overheal occurred, even without a recipe.
        if data["hp"] < data["max_hp"]:
            return data["amount"]
        if recipe is None or context.build != support_catalog()["build"]:
            return None
        target = data["target"]
        missing = data["max_hp"] - data["hp"] + data["amount"]
        # Some graphs heal the status caster or use the attached unit as provider.
        # Only bind the selectors identified by this particular graph's ports.
        entities = {recipe["owner"]: source, recipe["target"]: target}

        def stat(entity, key):
            if entity is None:
                raise ValueError("Provider unavailable")
            if key in context.stats[entity]:
                return context.stats[entity][key]
            if context.full_history and entity in context.fresh:
                defaults = support_catalog()["defaults"]
                stats = {
                    **defaults.get(context.units.get(entity), {}),
                    **defaults.get(self.classes.get(entity), {}),
                }
                return stats.get(key, 0)
            raise ValueError("Initial stat unavailable")

        def evaluate(value):
            if isinstance(value, int):
                return value
            op = value["op"]
            if op == "stat":
                return stat(entities[value["side"]], value["id"])
            if op == "stacks":
                entity = entities[value["side"]]
                effect = (
                    data.get("effect") if value["effect"] == "event_effect" else value["effect"]
                )
                if entity is None or effect is None:
                    raise ValueError("Status owner or effect unavailable")
                if not context.full_history or entity not in context.fresh:
                    raise ValueError("Initial status stacks are unavailable")
                return context.stacks(entity, effect)
            a, b = [evaluate(v) for v in value["args"]]
            if op == "add":
                return a + b
            if op == "mul":
                return a * b
            if op == "sub":
                return a - b
            if op == "div":
                return trunc_div(a, b)
            raise ValueError("Unsupported expression")

        try:
            amount = evaluate(recipe["amount"])
            if recipe["mode"] == "max_hp":
                amount = fix_to_int(fix_ratio(data["max_hp"], 100) * amount)
            elif recipe["mode"] == "missing_hp":
                amount = missing * amount // 100
            if not recipe["ignore_modifiers"]:
                increase = fix_ratio(stat(source, "pROS9yE0"))
                decrease = fix_ratio(stat(target, "1SDlVYuI"))
                amount = (amount * (4096 + increase) + 4095) // 4096
                if amount > 0:
                    amount = max(amount * (4096 - decrease) // 4096, 1)
            if amount >= 0 and min(amount, missing) == data["amount"]:
                return amount
        except (KeyError, TypeError, ValueError, ArithmeticError):
            pass
        return None

    def apply(self, event: Event, context: DamageContext, unit_teams: dict) -> list[tuple]:
        d, kind = event.data, event.kind
        self.mitigation.observe(event)
        self._observe_heal_cast(event)
        if kind in ("player", "class") and d.get("class_id"):
            self.classes[d["id"]] = d["class_id"]
        if kind == "source_started" or kind == "connection":
            # Lost history is unknown, not an observed shield expiry.
            for target in list(self.pools):
                self._drain(
                    target,
                    sum(v["components"]["Active shield"] for v in self.pools[target]),
                    "Unresolved",
                )
            self.__init__()
        elif kind == "support_record":
            record = d["record"]
            metric = "shield" if record in ("EDclDmMI", "KuFLDDDD") else "heal"
            if record in ("EDclDmMI", "hkIlI6PO", "SYuK7HPf"):
                self.providers[metric] = (d["id"], event.timestamp)
                self.receipts.pop(metric, None)
            else:
                provider = self.providers.pop(metric, None)
                if provider and provider[1] == event.timestamp:
                    self.receipts[metric] = (provider[0], d["id"], event.timestamp)
                else:
                    self.receipts.pop(metric, None)
        elif kind in ("health", "player", "unit") and "hp" in d:
            target = d["id"]
            self._align(target, d["shield"], "Unused shield" if kind == "health" else "Unresolved")
            self.health[target] = {k: d[k] for k in ("hp", "shield", "max_hp")}
        elif kind == "damage_health":
            self.damage_health = event
        elif kind in ("heal", "shield"):
            recipe = support_catalog()["actions"].get(d.get("action"))
            if context.build != support_catalog()["build"]:
                recipe = None
            source = self._provider(event, context, recipe)
            if kind == "shield":
                self._align(d["target"], max(0, d["shield"] - d["amount"]))
                value = entry(
                    source,
                    "Shield granted",
                    d["amount"],
                    {"Active shield": d["amount"]},
                    target=d["target"],
                )
                self.pools[d["target"]].append(value)
                metric = "shielding"
            else:
                requested = self._requested_heal(d, source, recipe, context)
                over = requested - d["amount"] if requested is not None else 0
                value = entry(
                    source,
                    d.get("action") or d.get("effect") or "unknown",
                    d["amount"] + over,
                    {"HP restored": d["amount"], "Overheal": over},
                    target=d["target"],
                    unknown_overheal=requested is None,
                )
                self._align(d["target"], d["shield"])
                metric = "healing"
            self.health[d["target"]] = {k: d[k] for k in ("hp", "shield", "max_hp")}
            return [(metric, value)] if d["target_team"] == "Friendly" else []
        elif kind == "damage":
            rows = []
            target, amount = d["target"], d["amount"]
            pending, self.damage_health = self.damage_health, None
            old = self.health.get(target, {})
            components = {"Unresolved": amount}
            shield_providers = Counter()
            if (
                pending
                and pending.timestamp == event.timestamp
                and all(pending.data[k] == d[k] for k in ("source", "target", "amount"))
            ):
                hp = pending.data["hp"]
                if "hp" in old and hp <= old["hp"] and not d["dodged"]:
                    lost = old["hp"] - hp
                    shield = old.get("shield")
                    candidates = set()
                    if shield is not None:
                        absorbed = min(shield, amount)
                        if max(0, old["hp"] - amount + absorbed) == hp:
                            candidates.add(absorbed)
                        if max(0, old["hp"] - amount) == hp:
                            candidates.add(0)
                    if lost <= amount and len(candidates) == 1:
                        absorbed = candidates.pop()
                        components = {
                            "Health lost": lost,
                            "Absorbed": absorbed,
                            "Overkill": amount - lost - absorbed,
                        }
                        shield_providers = self._drain(target, absorbed, "Absorbed")
                        old["shield"] = shield - absorbed
                    else:
                        components = {
                            "Health lost": min(lost, amount),
                            "Unresolved": max(0, amount - lost),
                        }
                        self._drain(
                            target,
                            sum(v["components"]["Active shield"] for v in self.pools[target]),
                            "Unresolved",
                        )
                        old.pop("shield", None)
                old["hp"] = hp
                self.health[target] = old
            else:
                # Do not reuse a pre-hit balance when the matching HP update is missing.
                self._drain(
                    target,
                    sum(v["components"]["Active shield"] for v in self.pools[target]),
                    "Unresolved",
                )
                self.health.pop(target, None)
            if d["in_combat"] and d["target_team"] == "Friendly":
                source = d.get("action") or d.get("effect") or d.get("ability") or "unknown"
                if not d["dodged"]:
                    value = entry(target, source, amount, components)
                    value["mitigation"] = self.mitigation.explain(event, context)
                    value["shield_providers"] = shield_providers
                    rows.append(("taken", value))
                if (
                    unit_teams.get(d["source"]) == "Enemy"
                    and d.get("ability")
                    and not d.get("effect")
                ):
                    rows.append(
                        (
                            "pressure",
                            entry(target, source, 1, {"Dodged" if d["dodged"] else "Landed": 1}),
                        )
                    )
            return rows
        return []
