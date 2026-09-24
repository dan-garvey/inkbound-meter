import copy
import json
from pathlib import Path

import pytest

from inkbound_meter.burn import BURN, BURN_STAT, KILL_BONUS
from inkbound_meter.components import DamageContext
from inkbound_meter.model import Meter, Player
from inkbound_meter.parser import Event, parse_line

from .helpers import broadcast

RECORDS = json.loads((Path(__file__).parent / "fixtures/burn_recovery.json").read_text())


def proc(entity, target, effect=BURN, kind="OnTurnEnd", timestamp="tick"):
    return Event(
        "status_proc",
        timestamp,
        {
            "id": entity,
            "target": target,
            "effect": effect,
            "proc": kind,
        },
    )


def setup(record, *, announce=True):
    record = copy.deepcopy(record)
    ctx = DamageContext()
    c = record["context"]
    ctx.build, ctx.full_history = c["build"], c["full_history"]
    ctx.fresh = set(c["fresh"])
    ctx.stats.update({int(k): v for k, v in c["stats"].items()})
    ctx.units = {int(k): v for k, v in c["units"].items()}
    ctx.statuses = {(v["target"], v["instance"]): v for v in c["statuses"]}
    meter = Meter()
    meter.apply(Event("run_create"))
    meter.apply(Event("connection", data={"connected": True, "seed": 1}))
    meter.apply(Event("combat_start", data={"zone": 100}))
    meter.damage_context = ctx
    event = Event(**record["event"])
    meter.current.players[event.data["source"]] = Player(event.data["source"])
    meter.support_context.health[event.data["target"]] = dict(record["health"])
    for bonus in record["bonuses"]:
        meter.apply(Event(**bonus))
    if announce:
        meter.apply(proc(event.data["target"], event.data["target"], timestamp=event.timestamp))
    return meter, event


def breakdown(meter, source):
    return meter.current.totals.breakdowns[source, "Burn_Damage_Action"]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: str(r["line"]))
def test_recorded_burn_ticks(record):
    meter, event = setup(record)
    meter.apply(event)
    b = breakdown(meter, event.data["source"])
    assert b.unresolved_damage == event.data["amount"]
    assert b.latest.get("predicted") == record["initial_prediction"]
    for following in record["following"]:
        meter.apply(Event(**following))
    expected = event.data["amount"] if record["expected_matched"] else 0
    assert b.matched_damage == expected
    assert b.matched_damage + b.unresolved_damage == event.data["amount"]
    assert b.hits == 1
    assert sum(b.credits.values()) == expected * 100
    assert (
        b.view()
        == meter.current.encounters[0]
        .totals.breakdowns[event.data["source"], "Burn_Damage_Action"]
        .view()
    )
    if expected:
        assert b.latest["predicted"] == event.data["amount"]
        assert b.view()["reasons"] == {}


def decay(record=RECORDS[0]):
    return Event(
        **next(
            e
            for e in record["following"]
            if e["kind"] == "status" and e["data"]["effect"] == BURN and "removed" in e["data"]
        )
    )


@pytest.mark.parametrize(
    "change",
    [
        {"source": 999},
        {"target": 999},
        {"instance": 999},
        {"removed": 10},
        {"removed": 0, "added": 4},
        {"removed": None},
        {"stacks": -1},
    ],
)
def test_unrelated_or_incomplete_stack_records_do_not_repair_a_hit(change):
    meter, event = setup(RECORDS[0])
    meter.apply(event)
    after = decay()
    data = {**after.data, **change}
    if data.get("removed") is None:
        data.pop("removed")
    meter.apply(Event("status", after.timestamp, data))
    assert breakdown(meter, event.data["source"]).matched_hits == 0


@pytest.mark.parametrize(
    "interruption",
    ["later_timestamp", "added_stacks", "turn_phase", "combat_end", "incinerate", "duplicate_hit"],
)
def test_ambiguous_late_or_non_turn_end_decay_is_not_used(interruption):
    meter, event = setup(RECORDS[0])
    meter.apply(event)
    after = decay()
    if interruption == "later_timestamp":
        after = Event("status", "later", after.data)
    elif interruption == "added_stacks":
        meter.apply(Event("status", event.timestamp, {**after.data, "added": 2, "removed": 0}))
    elif interruption == "turn_phase":
        meter.apply(Event("turn_phase", event.timestamp, {"zone": 100, "phase": "PlayerTurn"}))
    elif interruption == "combat_end":
        meter.apply(Event("combat_end", event.timestamp))
    elif interruption == "incinerate":
        meter.apply(
            proc(
                event.data["target"],
                event.data["source"],
                kind="OnBeingHit",
                timestamp=event.timestamp,
            )
        )
    else:
        # Even with two proc announcements, one removal cannot identify which
        # of two same-caster hits it belongs to. Never choose whichever fits.
        meter.apply(proc(event.data["target"], event.data["target"], timestamp=event.timestamp))
        meter.apply(event)
    meter.apply(after)
    assert breakdown(meter, event.data["source"]).matched_hits == 0


def test_proc_evidence_is_required_and_old_stats_are_frozen():
    meter, event = setup(RECORDS[0], announce=False)
    meter.apply(event)
    meter.apply(decay())
    assert breakdown(meter, event.data["source"]).matched_hits == 0
    meter, event = setup(RECORDS[0])
    meter.apply(event)
    meter.apply(
        Event(
            "stat", event.timestamp, {"id": event.data["source"], "stat": "K3G3pgjn", "value": 999}
        )
    )
    meter.apply(decay())
    assert breakdown(meter, event.data["source"]).matched_damage == 2552


def test_late_resolution_preserves_latest_hit_and_cannot_count_twice():
    meter, event = setup(RECORDS[0])
    meter.apply(event)
    meter.apply(Event("damage", event.timestamp, {**event.data, "target": 987, "amount": 7}))
    b = breakdown(meter, event.data["source"])
    latest = b.latest
    meter.apply(decay())
    meter.apply(decay())
    assert (b.hits, b.matched_hits, b.matched_damage, b.unresolved_damage) == (2, 1, 2552, 7)
    assert b.latest is latest and b.latest["target"] == 987
    assert meter.snapshot(scope="run")["party_damage"] == 2559


@pytest.mark.parametrize("invalid_confirmation", [False, True])
def test_kill_bonus_waits_for_confirmation_and_excludes_the_current_victim(invalid_confirmation):
    record = next(r for r in RECORDS if r["line"] == 409399)
    meter, event = setup(record)
    meter.apply(event)
    # A proc from this hit's own victim must affect subsequent ticks only.
    meter.apply(
        proc(
            event.data["source"],
            event.data["target"],
            effect=KILL_BONUS,
            kind="OnEnemyDied",
            timestamp=event.timestamp,
        )
    )
    meter.apply(decay(record))
    b = breakdown(meter, event.data["source"])
    assert b.matched_hits == 0
    # 43 -> 44 confirms the previous victim's +1. An equipment-sized jump does not.
    meter.apply(
        Event(
            "stat",
            "confirmation",
            {
                "id": event.data["source"],
                "stat": BURN_STAT,
                "value": 99 if invalid_confirmation else 44,
            },
        )
    )
    assert b.matched_hits == (0 if invalid_confirmation else 1)
    if not invalid_confirmation:
        assert b.latest["inputs"]["Burn damage"] == 44
        assert b.latest["confirmed_burn_bonus"] == 1
        meter.apply(
            Event(
                "stat", "next bonus", {"id": event.data["source"], "stat": BURN_STAT, "value": 45}
            )
        )
        assert b.matched_hits == 1 and b.latest["inputs"]["Burn damage"] == 44


def test_zero_base_burn_bonus_can_be_confirmed():
    record = copy.deepcopy(RECORDS[0])
    source = str(record["event"]["data"]["source"])
    record["context"]["stats"][source] = {}
    record["event"]["data"]["amount"] = 242  # (10 + 1) * 22 observed stacks
    meter, event = setup(record)
    meter.apply(proc(event.data["source"], 123, effect=KILL_BONUS, kind="OnEnemyDied"))
    meter.apply(event)
    meter.apply(decay(record))
    meter.apply(Event("stat", "later", {"id": event.data["source"], "stat": BURN_STAT, "value": 1}))
    assert breakdown(meter, event.data["source"]).matched_damage == 242


def test_regular_overkill_is_not_a_minimum_health_clamp():
    record = copy.deepcopy(RECORDS[1])
    meter, event = setup(record)
    meter.apply(event)
    meter.apply(decay(record))
    b = breakdown(meter, event.data["source"])
    assert event.data["amount"] > record["health"]["hp"]
    assert b.matched_damage == 3596
    assert "Minimum health limit" not in b.credits


def test_burn_reconstruction_failure_keeps_raw_damage(monkeypatch):
    meter, event = setup(RECORDS[0])
    meter.apply(event)

    def broken(_):
        raise ValueError("unmapped")

    monkeypatch.setattr(meter.burn, "_explain", broken)
    meter.apply(decay())
    b = breakdown(meter, event.data["source"])
    assert b.unresolved_damage == meter.snapshot(scope="run")["party_damage"] == 2552


def test_burn_proc_parser_ignores_subscribers_and_unmapped_procs():
    line = broadcast(
        "EventOnUnitStatusEffectAction-WorldStateChangeUnitStatusEffectAction-"
        "UnitEntityHandle:(EntityHandle:99)-ContextTargetUnitEntityHandle:(EntityHandle:99)-"
        "StatusEffect:Burn_StatusEffect-sXmQNYjg-ProcType:OnTurnEnd-SkipPause:False"
    )
    assert parse_line(line).data == proc(99, 99).data
    assert parse_line(line.replace("broadcasting", "handling")) is None
    assert parse_line(line.replace("OnTurnEnd", "OnDeath")) is None
