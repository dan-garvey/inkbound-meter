"""Reconcile Burn ticks with the game's delayed presentation of state changes.

The inspected Burn turn-end graph deals damage, then removes a fifth of that
caster's stacks. That removal records both the amount removed and the remainder.
The on-kill Burn vestige adds one damage; its announcement can precede its stat
broadcast. Neither correction derives an input by dividing the observed damage.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from .components import Breakdown, DamageContext, catalog
from .parser import Event

BURN = "Burn_StatusEffect"
BURN_STAT = "vBPDze3C"
KILL_BONUS = "VestigeAll_Epic_BurnDamageOnBurn_StatusEffect"


@dataclass
class Bonus:
    confirmed: bool | None = None


@dataclass
class Tick:
    event: Event
    context: DamageContext
    instance: int
    previous: dict
    breakdowns: tuple[Breakdown, ...]
    health: dict
    bonuses: tuple[Bonus, ...]
    decay: Event | None = None


class BurnTracker:
    def __init__(self):
        self.procs: dict[int, int] = defaultdict(int)
        self.bonuses: dict[int, deque[Bonus]] = defaultdict(deque)
        self.ticks: list[Tick] = []
        self.timestamp = ""
        self.ambiguous: set[tuple[int, int]] = set()

    def apply(self, event: Event, context: DamageContext) -> None:
        """Called before applying this record to the current damage context."""
        d, kind = event.data, event.kind
        if kind in (
            "source_started",
            "connection",
            "run_create",
            "run_end",
            "combat_start",
            "combat_end",
            "turn_phase",
            "parse_error",
        ):
            self.__init__()
            return
        # A decay is part of the same presentation batch as its hit. A later
        # turn/cleanse must never repair an old hit just because the values fit.
        if event.timestamp != self.timestamp:
            self.ambiguous.clear()
            self.timestamp = event.timestamp
        self.ticks = [t for t in self.ticks if t.decay or t.event.timestamp == event.timestamp]
        if kind == "status_proc":
            if d["effect"] == BURN:
                if d["proc"] == "OnTurnEnd":
                    self.procs[d["id"]] += 1
                else:
                    # Incinerate triggers Burn without the turn-end decay.
                    self.procs.pop(d["id"], None)
                    self.ticks = [t for t in self.ticks if t.event.data["target"] != d["id"]]
            elif d["effect"] == KILL_BONUS and d["proc"] == "OnEnemyDied":
                self.bonuses[d["id"]].append(Bonus())
        elif kind == "stat" and d["stat"] == BURN_STAT:
            pending = self.bonuses[d["id"]]
            old = context.stats.get(d["id"], {}).get(
                BURN_STAT, 0 if context.full_history and d["id"] in context.fresh else None
            )
            delta = d["value"] - old if old is not None else 0
            if 0 < delta <= len(pending):
                for _ in range(delta):
                    pending.popleft().confirmed = True
            else:
                # Equipment changes or incomplete history do not confirm a proc.
                for bonus in pending:
                    bonus.confirmed = False
                pending.clear()
        elif kind == "status" and d["effect"] == BURN:
            remaining = []
            for tick in self.ticks:
                hit = tick.event.data
                if tick.decay or (d["target"], d["source"]) != (hit["target"], hit["source"]):
                    remaining.append(tick)
                    continue
                removed = d.get("removed", 0)
                stacks = d["stacks"] + removed
                if (
                    d["instance"] == tick.instance
                    and removed > 0
                    and d["stacks"] >= 0
                    and removed == max(1, stacks // 5)
                    and event.timestamp == tick.event.timestamp
                ):
                    tick.decay = event
                    remaining.append(tick)
                # Any other same-caster Burn change makes this pairing ambiguous.
            self.ticks = remaining
        elif kind == "damage":
            if any(
                not t.decay
                and (t.event.data["target"], t.event.data["source"]) == (d["target"], d["source"])
                for t in self.ticks
            ):
                self.ambiguous.add((d["target"], d["source"]))
            self.ticks = [
                t
                for t in self.ticks
                if t.decay
                or (t.event.data["target"], t.event.data["source"]) != (d["target"], d["source"])
            ]
        elif kind == "unit_removed":
            self.procs.pop(d["id"], None)
            self.ticks = [
                t
                for t in self.ticks
                if d["id"] not in (t.event.data["source"], t.event.data["target"])
            ]
        self._resolve()

    def capture(
        self,
        event: Event,
        context: DamageContext,
        result: dict,
        breakdowns: tuple[Breakdown, ...],
        health: dict,
    ) -> None:
        d = event.data
        if d.get("action") != "Burn_Damage_Action":
            return
        procs = self.procs.get(d["target"], 0)
        if procs:
            self.procs[d["target"]] -= 1
        if (
            not procs
            or not event.timestamp
            or result["status"] == "matched"
            or context.build != catalog()["build"]
            or (d["target"], d["source"]) in self.ambiguous
        ):
            return
        instances = [
            v
            for v in context.statuses.values()
            if v["target"] == d["target"] and v["source"] == d["source"] and v["effect"] == BURN
        ]
        if len(instances) != 1:
            return
        # Freeze only the two entities needed by Burn. Future stat/status updates
        # must not leak into the historical hit's other inputs.
        snapshot = DamageContext()
        snapshot.build, snapshot.full_history = context.build, context.full_history
        entities = {d["source"], d["target"]}
        snapshot.fresh = context.fresh & entities
        snapshot.stats.update({k: dict(v) for k, v in context.stats.items() if k in entities})
        snapshot.statuses = {k: dict(v) for k, v in context.statuses.items() if k[0] in entities}
        self.ticks.append(
            Tick(
                event,
                snapshot,
                instances[0]["instance"],
                result,
                breakdowns,
                dict(health),
                tuple(self.bonuses[d["source"]]),
            )
        )

    def _resolve(self) -> None:
        waiting = []
        for tick in self.ticks:
            if any(b.confirmed is False for b in tick.bonuses):
                continue
            if not tick.decay or any(b.confirmed is None for b in tick.bonuses):
                waiting.append(tick)
                continue
            # Reconstruction failures must not interrupt raw damage collection.
            try:
                result = self._explain(tick)
            except (ValueError, KeyError, TypeError, ArithmeticError, AssertionError):
                continue
            if result["status"] == "matched":
                for breakdown in tick.breakdowns:
                    breakdown.resolve(tick.previous, result, tick.event.data["amount"])
        self.ticks = waiting

    @staticmethod
    def _explain(tick: Tick) -> dict:
        d = tick.event.data
        decay = tick.decay.data
        stacks = decay["stacks"] + decay["removed"]
        tick.context.statuses[d["target"], tick.instance] = {**decay, "stacks": stacks}
        if tick.bonuses:
            stats = tick.context.stats[d["source"]]
            stats[BURN_STAT] = stats.get(BURN_STAT, 0) + len(tick.bonuses)
        result = tick.context.explain(tick.event)
        result["stack_evidence"] = {
            "instance": tick.instance,
            "removed": decay["removed"],
            "remaining": decay["stacks"],
            "timestamp": tick.decay.timestamp,
        }
        if tick.bonuses:
            result["confirmed_burn_bonus"] = len(tick.bonuses)
        # DamageHelper clamps near-death units (e.g. Kraken tentacles) to their
        # explicit minimum HP. Ordinary overkill is deliberately not clamped.
        floor = tick.context.stats[d["target"]].get("frNtAO1z", 0)
        if (
            floor > 0
            and {"hp", "shield"} <= tick.health.keys()
            and "predicted" in result
            and tick.health["hp"] >= floor
        ):
            cap = tick.health["hp"] - floor + tick.health["shield"]
            if result["predicted"] > cap:
                credits = {c["name"]: round(c["damage"] * 100) for c in result["components"]}
                credits["Minimum health limit"] = (cap - result["predicted"]) * 100
                result.update(
                    predicted=cap,
                    components=[{"name": k, "damage": v / 100} for k, v in credits.items()],
                )
                if cap == d["amount"]:
                    result.update(status="matched", credits=credits, order=list(credits))
                    result.pop("reason", None)
                    result.pop("difference", None)
                else:
                    result.update(
                        status="mismatch",
                        reason="Formula differs from logged hit",
                        difference=d["amount"] - cap,
                    )
                    result.pop("credits", None)
                    result.pop("order", None)
                result["inputs"].update(
                    {
                        "Target: Health before hit": tick.health["hp"],
                        "Target: Shield before hit": tick.health["shield"],
                        "Target: Minimum health": floor,
                    }
                )
        return result
