"""Attribute observed incoming damage prevention without changing damage totals.

Only a complete reconstruction matching the broadcast earns pre-shield credit.
Percent stages carry the game's rounding; named contributors share their stage's
credit proportionally. Flat reduction credits ordinary Blur before its vestige
bonus, capped by damage still present. Shield absorption is measured separately.
"""

from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from importlib.resources import files

from .components import catalog, fix_round


@lru_cache(maxsize=1)
def mitigation_catalog():
    return json.loads(
        files("inkbound_meter").joinpath("mitigation_catalog.json").read_text("utf-8")
    )


def _split(amount, weights):
    """Allocate integer hundredths proportionally, retaining the exact total."""
    total = sum(weights.values())
    shares = {k: amount * v // total for k, v in weights.items()}
    remainder = amount - sum(shares.values())
    order = sorted(weights, key=lambda k: (-(amount * weights[k] % total), k))
    for key in order[:remainder]:
        shares[key] += 1
    return shares


class MitigationContext:
    def __init__(self):
        self.zones = {}
        self.teams = {}

    def observe(self, event):
        d = event.data
        if event.kind in ("source_started", "connection"):
            self.__init__()
        elif event.kind in ("player", "unit", "combat_zone"):
            if "zone" in d:
                self.zones[d["id"]] = d["zone"]
            if event.kind == "player":
                self.teams[d["id"]] = "Friendly"
            elif "team" in d:
                self.teams[d["id"]] = d["team"]
        elif event.kind == "unit_removed":
            self.zones.pop(d["id"], None)
            self.teams.pop(d["id"], None)

    def _contributors(self, context, entity, stat):
        known = Counter()
        for status in context.statuses.values():
            recipe = mitigation_catalog()["defenses"].get(status["effect"])
            if status["target"] != entity or not recipe:
                continue
            for part in recipe["entries"]:
                if part["stat"] != stat:
                    continue
                if part["source_has"] and not context.stacks(status["source"], part["source_has"]):
                    continue
                known[part["label"]] += part["value"] * (
                    status["stacks"] if recipe["per_stack"] else 1
                )
        return known

    def _credit(self, context, event, stage):
        key, label, amount = stage["stat"], stage["name"], stage["amount"]
        if not amount:
            return {}
        if key == "0QnywPkl":
            label = "Attacker damage reduction"
        entity = event.data["source" if key == "0QnywPkl" else "target"]
        points = context.stats[entity].get(key, 0)
        known = self._contributors(context, entity, key) if key else Counter()
        # A net stat below the known contributions can mean a cap, a negative
        # modifier, or missing status history. Do not invent item-level shares.
        if not known or min(known.values()) < 0 or sum(known.values()) > points:
            return {label: amount * 100}
        if key == "TWM45OpA":
            left, result = amount, {}
            # Blur's base effect gets credit before its conditional vestige boost.
            for name, value in sorted(known.items(), key=lambda p: (p[0] != "Blur", p[0])):
                used = min(left, value)
                result[name] = used * 100
                left -= used
            if left:
                result[label] = left * 100
            return result
        known[label] += points - sum(known.values())
        return _split(amount * 100, {k: v for k, v in known.items() if v})

    def explain(self, event, context):
        d, cat = event.data, mitigation_catalog()

        def unavailable(reason):
            return {"status": "unavailable", "reason": reason, "credits": {}, "prevented": 0}

        if context.build != cat["build"]:
            return unavailable("Game build is missing or not mapped")
        if d["dodged"]:
            return unavailable("Dodged attack")
        if d.get("action") in cat["literal_actions"]:
            return {"status": "matched", "credits": {}, "prevented": 0, "before": d["amount"]}
        spec = cat["actions"].get(d.get("action")) or catalog()["actions"].get(d.get("action"))
        if not spec:
            return unavailable("Incoming attack formula is not mapped")
        if spec.get("self") and d["source"] != d["target"]:
            return unavailable("Self-damage source differs from target")
        if not context.full_history or not {d["source"], d["target"]} <= context.fresh:
            return unavailable("Initial combat stats are missing")
        ability = catalog()["abilities"].get(d.get("ability"))
        direct_multiplier = None
        if ability and ability["range_mode"] in (4, 11):
            if not spec["direct"]:
                return unavailable("Indirect targeted attack scaling is not mapped")
            zone = self.zones.get(d["source"], -1)
            if zone < 0 or self.zones.get(d["target"]) != zone:
                return unavailable("Combat party size is missing")
            count = sum(
                self.teams.get(entity) == "Friendly" and z == zone
                for entity, z in self.zones.items()
            )
            scale = cat["direct_attack_raw"].get(str(count))
            if scale is None:
                return unavailable("Combat party size is unsupported")
            if context.stacks(d["target"], cat["easy_mode_status"]):
                scale = (scale * cat["easy_mode_raw"]) >> 12
            direct_multiplier = fix_round(scale * 100)
        try:
            result = context.explain(event, incoming_spec=spec, direct_multiplier=direct_multiplier)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return unavailable("Incoming formula inputs are unavailable")
        if result["status"] != "matched":
            return unavailable(result["reason"])
        credits = Counter()
        for stage in result["prevention"]:
            credits.update(self._credit(context, event, stage))
        prevented = result["before_defenses"] - d["amount"]
        assert all(v >= 0 for v in credits.values())
        assert sum(credits.values()) == prevented * 100
        return {
            "status": "matched",
            "before": result["before_defenses"],
            "prevented": prevented,
            "credits": {k: v for k, v in credits.items() if v},
        }
