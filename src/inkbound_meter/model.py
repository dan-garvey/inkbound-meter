"""Pure aggregation shared by file replay, the live reader and the overlay."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .burn import BurnTracker
from .components import Breakdown, DamageContext
from .parser import Event
from .party import PartyState
from .support import SupportContext, SupportTotals

# Verified against community mappings and installed class assets; preserve unknown IDs.
CLASS_NAMES = {
    "C01": "Magma Miner",
    "C02": "Mosscloak",
    "C03": "Clairvoyant",
    "C04": "Weaver",
    "C05": "Obelisk",
    "C07": "Star Captain",
    "C08": "Chainbreaker",
    "C09": "Godkeeper",
}


def source_label(raw: str) -> str:
    if raw == "unknown":
        return "Unknown source"
    raw = raw.replace("FrostBite", "Frostbite")
    label = re.sub(r"(?:_?(?:ActionData|AbilityData|StatusEffect|Action|Damage))+$", "", raw)
    label = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", label)
    return label.replace("_", " ").strip() or raw


def loadout_label(raw: str) -> str:
    """Turn the asset identity in a draft broadcast into a compact local label."""
    raw = re.sub(r"(?:_?(?:Equip|AbilityData|AbilityUpgrade))$", "", raw)
    prefix = ""
    if raw.startswith("VestigeAll_"):
        prefix, raw = "Vestige: ", raw.removeprefix("VestigeAll_")
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
    return prefix + (raw.replace("_", " ").strip() or "Unknown")


@dataclass
class Player:
    entity_id: int
    name: str = ""
    class_id: str = ""


@dataclass
class Totals:
    damage: dict[int, Counter] = field(default_factory=lambda: defaultdict(Counter))
    breakdowns: dict[tuple[int, str], Breakdown] = field(default_factory=dict)
    hits: int = 0

    def add(
        self, source: int, action: str, amount: int, explanation: dict | None = None
    ) -> Breakdown | None:
        self.damage[source][action] += amount
        self.hits += 1
        if explanation is not None:
            breakdown = self.breakdowns.setdefault((source, action), Breakdown())
            breakdown.add(explanation, amount)
            return breakdown
        return None

    def view(
        self,
        players: dict[int, Player],
        unit_teams: dict[int, str] | None = None,
        *,
        rounds: int = 0,
        will_spent: Counter | None = None,
    ) -> dict:
        unit_teams = unit_teams or {}
        will_spent = will_spent or Counter()
        party_total = sum(sum(v.values()) for k, v in self.damage.items() if k in players)
        rows = []
        for entity, player in players.items():
            sources = self.damage.get(entity, {})
            total = sum(sources.values())
            rows.append(
                {
                    "id": entity,
                    "name": player.name or f"Player {entity}",
                    "class": CLASS_NAMES.get(player.class_id, player.class_id),
                    "damage": total,
                    "share": total / party_total if party_total else 0.0,
                    "per_round": total / rounds if rounds else None,
                    "rounds": rounds,
                    "will_spent": will_spent[entity],
                    "per_will": total / will_spent[entity] if will_spent[entity] else None,
                    "sources": [
                        {
                            "id": key,
                            "name": source_label(key),
                            "damage": value,
                            "share": value / total if total else 0.0,
                            "breakdown": self.breakdowns.get((entity, key), Breakdown()).view(),
                        }
                        for key, value in sorted(
                            sources.items(), key=lambda pair: (-pair[1], pair[0])
                        )
                    ],
                }
            )
        rows.sort(key=lambda row: (-row["damage"], row["id"]))
        non_party = sum(
            sum(v.values())
            for k, v in self.damage.items()
            if k not in players and unit_teams.get(k) in ("Enemy", "Neutral")
        )
        unknown = (
            sum(sum(v.values()) for k, v in self.damage.items() if k not in players) - non_party
        )
        return {
            "players": rows,
            "party_damage": party_total,
            "unattributed_damage": unknown,
            "non_party_damage": non_party,
            "damage_events": self.hits,
            "rounds": rounds,
            "will_spent": sum(will_spent[entity] for entity in players),
            "per_round": party_total / rounds if rounds else None,
            "per_will": party_total / sum(will_spent[entity] for entity in players)
            if sum(will_spent[entity] for entity in players)
            else None,
        }


@dataclass
class Encounter:
    number: int
    zone: int | None
    partial: bool = False
    active: bool = True
    totals: Totals = field(default_factory=Totals)
    support: SupportTotals = field(default_factory=SupportTotals)
    rounds: int = 0
    will_spent: Counter = field(default_factory=Counter)
    phase: str = ""
    changes: list[dict] = field(default_factory=list)


@dataclass
class Run:
    number: int
    seed: int | None
    partial: bool
    outcome: str = "in_progress"
    players: dict[int, Player] = field(default_factory=dict)
    unit_teams: dict[int, str] = field(default_factory=dict)
    encounters: list[Encounter] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    support: SupportTotals = field(default_factory=SupportTotals)
    support_available: bool = False
    rounds: int = 0
    will_spent: Counter = field(default_factory=Counter)
    pending_loadout_changes: list[dict] = field(default_factory=list)


class Meter:
    def __init__(self) -> None:
        self.runs: list[Run] = []
        self.current: Run | None = None
        self.in_run = False
        self.pending_new_run = False
        self.parse_errors = 0
        self.last_error = ""
        self.pending_players: dict[int, Player] = {}
        self.pending_units: dict[int, str] = {}
        self.pending_loadout_changes: list[dict] = []
        self.damage_context = DamageContext()
        self.burn = BurnTracker()
        self.support_context = SupportContext()
        self.party = PartyState()

    def _finish(self, outcome: str) -> None:
        if self.current and self.in_run:
            if self.current.outcome == "in_progress":
                self.current.outcome = outcome
            if self.current.encounters:
                self.current.encounters[-1].active = False
        self.in_run = False

    def _begin(
        self, seed: int | None = None, *, partial: bool = True, use_pending: bool = False
    ) -> Run:
        self._finish("interrupted")
        run = Run(len(self.runs) + 1, seed, partial)
        if use_pending:
            run.players = self.pending_players
            run.unit_teams = self.pending_units
            run.pending_loadout_changes = self.pending_loadout_changes
        self.pending_players = {}
        self.pending_units = {}
        self.pending_loadout_changes = []
        self.runs.append(run)
        self.current = run
        self.in_run = True
        return run

    def apply(self, event: Event) -> None:
        self.party.apply(event)
        self.burn.apply(event, self.damage_context)
        previous_health = dict(self.support_context.health.get(event.data.get("target"), {}))
        self.damage_context.apply(event)
        data = event.data
        kind = event.kind
        teams = self.current.unit_teams if self.in_run and self.current else self.pending_units
        additions = self.support_context.apply(event, self.damage_context, teams)
        if self.current and kind in ("heal", "shield", "health", "support_record"):
            self.current.support_available = True
        if additions and (self.in_run or data.get("in_combat")):
            run = self.current if self.in_run else self._begin(use_pending=True)
            assert run is not None
            if data.get("in_combat") and not run.encounters:
                run.encounters.append(Encounter(1, None, partial=True))
            for metric, value in additions:
                run.support.add(metric, value)
                # Between-fight support belongs to the run, not the last encounter.
                if data.get("in_combat") and run.encounters:
                    run.encounters[-1].support.add(metric, value)
        if kind == "parse_error":
            self.parse_errors += 1
            self.last_error = data["message"]
        elif kind == "source_started":
            self._finish("interrupted")
            self.pending_new_run = False
            self.pending_players = {}
            self.pending_units = {}
            self.pending_loadout_changes = []
        elif kind == "run_create":
            self.pending_new_run = True
        elif kind == "connection":
            if data["connected"]:
                run = self._begin(data["seed"], partial=not self.pending_new_run)
                run.support_available = data.get("support_version", 0) >= 3
                self.pending_new_run = False
            else:
                self._finish("left")
        elif kind in ("player", "class"):
            if data["id"] < 0:
                return
            players = self.current.players if self.in_run and self.current else self.pending_players
            # An animation class assignment alone is not evidence of a party member.
            if kind == "class" and data["id"] not in players:
                return
            player = players.setdefault(data["id"], Player(data["id"]))
            if data.get("name"):
                player.name = data["name"]
            if data.get("class_id") not in (None, "NONE", ""):
                player.class_id = data["class_id"]
        elif kind == "unit":
            units = self.current.unit_teams if self.in_run and self.current else self.pending_units
            units[data["id"]] = data["team"]
        elif kind == "loadout":
            changes = (
                self.current.pending_loadout_changes
                if self.in_run and self.current
                else self.pending_loadout_changes
            )
            changes.append({**data, "label": loadout_label(data["item"])})
        elif kind == "combat_start":
            run = self.current if self.in_run else self._begin(use_pending=True)
            assert run is not None
            if run.encounters and run.encounters[-1].zone == data["zone"]:
                return  # The same combat can be announced repeatedly.
            if run.encounters:
                run.encounters[-1].active = False
            run.encounters.append(
                Encounter(
                    len(run.encounters) + 1,
                    data["zone"],
                    changes=list(run.pending_loadout_changes),
                )
            )
            run.pending_loadout_changes.clear()
        elif kind == "combat_end":
            if self.current and self.current.encounters:
                self.current.encounters[-1].active = False
        elif kind == "turn_phase":
            if (
                self.current
                and self.in_run
                and self.current.encounters
                and self.current.encounters[-1].zone == data["zone"]
            ):
                encounter = self.current.encounters[-1]
                # One StartPlayerTurn represents one party round. Repeated
                # broadcasts do not create another round.
                if data["phase"] == "StartPlayerTurn" and encounter.phase != data["phase"]:
                    encounter.rounds += 1
                    self.current.rounds += 1
                encounter.phase = data["phase"]
        elif kind == "resource":
            # Parser-8 journals retain the current value but not the prior one.
            # They are replayed before the reader can enrich an intact log, so
            # leave their efficiency denominator unknown rather than failing or
            # guessing a cost.
            spent = data.get("previous", data["value"]) - data["value"]
            if (
                data["resource"] == "ManaPoints"
                and spent > 0
                and self.current
                and self.in_run
                and data["id"] in self.current.players
            ):
                self.current.will_spent[data["id"]] += spent
                if self.current.encounters and self.current.encounters[-1].active:
                    self.current.encounters[-1].will_spent[data["id"]] += spent
        elif kind == "run_end":
            if self.current and self.in_run and self.current.outcome == "in_progress":
                self.current.outcome = "victory" if data["won"] else "defeat"
                if self.current.encounters:
                    self.current.encounters[-1].active = False
        elif kind == "damage":
            # A non-dodged zero is the server's immunity/phase-state outcome,
            # not a dealt-damage hit. It cannot contribute to totals or have a
            # truthful component breakdown.
            if (
                not data["in_combat"]
                or data["target_team"] != "Enemy"
                or data["dodged"]
                or data["amount"] == 0
            ):
                return
            run = self.current if self.in_run else self._begin(use_pending=True)
            assert run is not None
            if not run.encounters:
                run.encounters.append(Encounter(1, None, partial=True))
            source = data["action"] or data["effect"] or data["ability"] or "unknown"
            try:
                explanation = self.damage_context.explain(event)
            except (ValueError, KeyError, TypeError, ArithmeticError, AssertionError) as exc:
                # A new/unmapped damage shape must never stop the damage meter.
                explanation = {
                    "status": "unavailable",
                    "observed": data["amount"],
                    "reason": "Reconstruction could not be evaluated",
                    "error_type": type(exc).__name__,
                }
            breakdowns = (
                run.totals.add(data["source"], source, data["amount"], explanation),
                run.encounters[-1].totals.add(data["source"], source, data["amount"], explanation),
            )
            self.burn.capture(event, self.damage_context, explanation, breakdowns, previous_health)

    def snapshot(self, *, scope: str = "encounter", run_index: int = -1) -> dict:
        if scope not in ("encounter", "run"):
            raise ValueError("scope must be encounter or run")
        base = {"scope": scope, "parse_errors": self.parse_errors, "last_error": self.last_error}
        if not self.runs:
            return {
                **base,
                "run": None,
                "encounter": None,
                "partial": False,
                "active": False,
                "outcome": "waiting",
                **Totals().view({}),
                "metrics": SupportTotals().view({}, rounds=0, will_spent=Counter()),
                "support_available": True,
                "encounter_history": [],
            }
        run = self.runs[run_index]
        encounter = run.encounters[-1] if run.encounters else None
        totals = run.totals if scope == "run" else encounter.totals if encounter else Totals()
        support = (
            run.support if scope == "run" else encounter.support if encounter else SupportTotals()
        )
        rounds = run.rounds if scope == "run" else encounter.rounds if encounter else 0
        will_spent = (
            run.will_spent if scope == "run" else encounter.will_spent if encounter else Counter()
        )
        encounter_history = []
        for recorded in run.encounters:
            recorded_damage = recorded.totals.view(
                run.players,
                run.unit_teams,
                rounds=recorded.rounds,
                will_spent=recorded.will_spent,
            )
            recorded_support = recorded.support.view(
                run.players, rounds=recorded.rounds, will_spent=recorded.will_spent
            )
            encounter_history.append(
                {
                    "number": recorded.number,
                    "active": recorded.active,
                    "rounds": recorded.rounds,
                    "will_spent": sum(recorded.will_spent.values()),
                    "damage": recorded_damage["party_damage"],
                    "metrics": {
                        metric: values["party_total"] for metric, values in recorded_support.items()
                    },
                    "changes": [
                        {
                            "player": run.players.get(change["id"], Player(change["id"])).name
                            or f"Player {change['id']}",
                            "kind": change["kind"],
                            "label": change["label"],
                            **({"ability": change["ability"]} if change.get("ability") else {}),
                        }
                        for change in recorded.changes
                    ],
                }
            )
        return {
            **base,
            "run": run.number,
            "seed": run.seed,
            "encounter": encounter.number if encounter else None,
            "encounter_count": len(run.encounters),
            "outcome": run.outcome,
            "partial": run.partial
            or bool(scope == "encounter" and encounter and encounter.partial),
            "active": bool(encounter and encounter.active),
            **totals.view(
                run.players,
                run.unit_teams,
                rounds=rounds,
                will_spent=will_spent,
            ),
            "metrics": support.view(
                run.players,
                rounds=rounds,
                will_spent=will_spent,
            ),
            "support_available": run.support_available,
            "encounter_history": encounter_history,
        }

    def report(self) -> dict:
        return {
            "metric": "logged_damage",
            "component_method": "calculation_order_waterfall; exact matches only",
            "support_method": "provider records and inspected ownership; shield spending FIFO; "
            "overheal reconstructed only with matching formulas and observed stats",
            "parse_errors": self.parse_errors,
            "runs": [self.snapshot(scope="run", run_index=i) for i in range(len(self.runs))],
        }
