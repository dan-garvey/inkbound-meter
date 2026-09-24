from pathlib import Path

import pytest

from inkbound_meter.model import Meter
from inkbound_meter.parser import Event, ParseError, parse_line, parse_safe
from inkbound_meter.service import replay
from inkbound_meter.support import entry

from .helpers import begin, broadcast, damage

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_encounter_has_independently_summed_player_totals():
    meter = replay(FIXTURES / "first_encounter.txt")
    report = meter.snapshot(scope="run")
    assert report["parse_errors"] == 0
    assert report["partial"] is False
    assert report["encounter_count"] == 1
    assert report["party_damage"] == 5522  # 1303 + 2634 + 1585 from 70 broadcasts.
    assert report["damage_events"] == 70
    assert {p["id"]: p["damage"] for p in report["players"]} == {14: 1303, 9: 2634, 11: 1585}
    assert sum(p["share"] for p in report["players"]) == pytest.approx(1)
    assert not meter.snapshot()["active"]


def test_identical_same_timestamp_procs_are_three_distinct_hits():
    meter = Meter()
    begin(meter)
    meter.apply(Event("player", data={"id": 9, "name": "Player Two"}))
    for line in (FIXTURES / "repeated_procs.txt").read_text().splitlines():
        if event := parse_line(line):
            meter.apply(event)
    assert meter.snapshot()["party_damage"] == 288
    assert meter.snapshot()["damage_events"] == 3


def test_only_damage_broadcast_counts_not_diagnostic_or_subscriber():
    line = damage()
    assert (
        parse_line(line.replace("[EventSystem] broadcasting", "TargetingSystem handling event:"))
        is None
    )
    assert parse_line("Client unit state damaging unit (EntityHandle:99). Damage Amount-25") is None
    assert parse_line("Combat Simulation completed in 6 milliseconds") is None
    assert parse_line("Preview predicted DamageAmount:25") is None


@pytest.mark.parametrize(
    "options",
    [
        {"TargetUnitTeam": "Friendly"},
        {"IsInActiveCombat": "False"},
        {"WasDodged": "True"},
    ],
)
def test_non_enemy_inactive_and_dodged_damage_excluded(options):
    meter = Meter()
    begin(meter)
    meter.apply(parse_line(damage(**options)))
    assert meter.snapshot()["party_damage"] == 0


def test_zero_damage_phase_or_immunity_hit_is_not_recorded():
    meter = Meter()
    begin(meter)
    meter.apply(parse_line(damage(0, action="Whirlwind_Action")))
    snapshot = meter.snapshot()
    assert snapshot["party_damage"] == 0
    assert snapshot["damage_events"] == 0
    assert snapshot["players"][0]["sources"] == []


def test_dot_critical_zero_and_delayed_attribution():
    meter = Meter()
    begin(meter)
    poison = parse_line(
        damage(
            40,
            source=2,
            action="Poison_Damage_Action",
            AbilityData="(none)",
            StatusEffectData="StatusEffectData-Poison_StatusEffect (fixture)",
        )
    )
    assert poison.data["ability"] is None
    assert poison.data["effect"] == "Poison_StatusEffect"
    meter.apply(poison)
    assert meter.snapshot()["unattributed_damage"] == 40
    meter.apply(Event("player", data={"id": 2, "name": "Late Arrival"}))
    meter.apply(parse_line(damage(60, source=2, IsCriticalHit="True")))
    meter.apply(parse_line(damage(0)))
    snap = meter.snapshot()
    assert snap["unattributed_damage"] == 0
    assert snap["party_damage"] == 100
    assert snap["players"][0]["share"] == 1
    assert {s["name"] for s in snap["players"][0]["sources"]} == {"Bonk", "Poison"}


def test_enemy_self_damage_kept_separate_from_unknown_and_party():
    meter = Meter()
    begin(meter)
    meter.apply(Event("unit", data={"id": 70, "team": "Enemy"}))
    meter.apply(parse_line(damage(50, source=70, action="HealthLink_Action")))
    meter.apply(parse_line(damage(10, source=71)))
    meter.apply(parse_line(damage(20)))
    snap = meter.snapshot()
    assert (snap["party_damage"], snap["non_party_damage"], snap["unattributed_damage"]) == (
        20,
        50,
        10,
    )
    assert snap["players"][0]["share"] == 1


def test_encounter_end_retains_last_fight_and_start_resets_only_encounter():
    meter = Meter()
    begin(meter)
    meter.apply(parse_line(damage(30)))
    meter.apply(Event("combat_end"))
    assert meter.snapshot()["party_damage"] == 30
    assert not meter.snapshot()["active"]
    meter.apply(Event("combat_start", data={"zone": 100}))
    meter.apply(Event("combat_start", data={"zone": 100}))
    meter.apply(parse_line(damage(12)))
    assert meter.snapshot()["encounter"] == 2
    assert meter.snapshot()["party_damage"] == 12
    assert meter.snapshot(scope="run")["party_damage"] == 42


def test_encounter_history_tracks_per_round_values_and_observed_loadout_changes():
    meter = Meter()
    begin(meter)
    meter.apply(Event("turn_phase", data={"zone": 99, "phase": "StartPlayerTurn"}))
    meter.apply(parse_line(damage(30)))
    meter.apply(
        parse_line(
            broadcast(
                "EventOnItemDrafted-WorldStateChangeItemDrafted-"
                "PlayerUnitHandle:(EntityHandle:1)-"
                "ItemData:EquipmentData-VestigeAll_Epic_SmiteAll_Equip (fixture)"
            )
        )
    )
    meter.apply(
        parse_line(
            broadcast(
                "EventOnAbilityUpgraded-WorldStateChangeAbilityUpgraded-"
                "SourceEntityHandle:(EntityHandle:1)-"
                "AbilityData:AbilityData-Invocation_AbilityData (fixture)-"
                "AbilityUpgradeData:AbilityUpgradeData-Invocation_Epic_SmiteAgain_AbilityUpgrade "
                "(fixture)"
            )
        )
    )
    meter.apply(Event("combat_end"))
    meter.apply(Event("combat_start", data={"zone": 100}))
    meter.apply(Event("turn_phase", data={"zone": 100, "phase": "StartPlayerTurn"}))
    meter.apply(parse_line(damage(50)))
    history = meter.snapshot(scope="run")["encounter_history"]
    assert [(entry["number"], entry["damage"], entry["rounds"]) for entry in history] == [
        (1, 30, 1),
        (2, 50, 1),
    ]
    assert history[0]["changes"] == []
    assert history[1]["changes"] == [
        {"player": "Player One", "kind": "gear", "label": "Vestige: Epic Smite All"},
        {
            "player": "Player One",
            "kind": "aspect",
            "label": "Invocation Epic Smite Again",
            "ability": "Invocation_AbilityData",
        },
    ]


def test_run_end_repeats_and_reused_entity_ids_do_not_mix_runs():
    meter = Meter()
    begin(meter)
    meter.apply(parse_line(damage(30)))
    for _ in range(2):
        meter.apply(Event("run_end", data={"won": True}))
    meter.apply(Event("connection", data={"connected": False, "seed": 1234}))
    meter.apply(Event("player", data={"id": 1, "name": "Hub Player"}))
    begin(meter)  # Same seed and player handle, a genuinely new run.
    meter.apply(parse_line(damage(7)))
    assert len(meter.runs) == 2
    first, second = meter.report()["runs"]
    assert first["outcome"] == "victory"
    assert first["party_damage"] == 30
    assert first["players"][0]["name"] == "Player One"
    assert second["party_damage"] == 7


def test_resumed_connection_is_explicitly_partial_and_not_merged_by_seed():
    meter = Meter()
    begin(meter)
    meter.apply(parse_line(damage(50)))
    meter.apply(Event("source_started"))
    meter.apply(Event("connection", data={"connected": True, "seed": 1234}))
    meter.apply(Event("player", data={"id": 1, "name": "Player One"}))
    meter.apply(parse_line(damage(10)))
    assert len(meter.runs) == 2
    assert meter.snapshot()["partial"]
    assert meter.snapshot()["party_damage"] == 10
    assert meter.report()["runs"][0]["party_damage"] == 50


def test_mid_log_capture_keeps_early_identity_and_is_partial():
    meter = Meter()
    meter.apply(Event("player", data={"id": 1, "name": "Observed Player"}))
    meter.apply(parse_line(damage()))
    assert meter.snapshot()["partial"]
    assert meter.snapshot()["party_damage"] == 25


def test_solo_or_guest_new_run_vote_identifies_a_fresh_run():
    meter = Meter()
    meter.apply(
        parse_line(broadcast("EventOnNewRunVoteFinished-WorldStateChangeNewRunVoteFinished"))
    )
    meter.apply(Event("connection", data={"connected": True, "seed": 1234}))
    meter.apply(Event("player", data={"id": 1, "name": "Player One"}))
    meter.apply(Event("combat_start", data={"zone": 99}))
    meter.apply(parse_line(damage(30)))
    assert not meter.snapshot()["partial"]
    assert meter.snapshot()["party_damage"] == 30


def test_player_names_can_contain_unicode_spaces_numbers_and_punctuation():
    event = parse_line("0T12:00:00 01 I Zoë's #2 Mage (EntityHandle:123) is playing ability Bonk")
    assert event.data == {"id": 123, "name": "Zoë's #2 Mage"}


def test_malformed_damage_is_visible_diagnostic_without_raw_record():
    raw = damage().replace("DamageAmount:25", "DamageAmount:NaN")
    with pytest.raises(ParseError):
        parse_line(raw)
    event = parse_safe(raw)
    assert event.kind == "parse_error"
    assert "DamageAmount" in event.data["message"]
    assert "EntityHandle" not in str(event.data)
    meter = Meter()
    meter.apply(event)
    assert meter.snapshot()["parse_errors"] == 1


def test_negative_amount_is_rejected_and_empty_totals_do_not_divide_by_zero():
    assert parse_safe(damage(-1)).kind == "parse_error"
    meter = Meter()
    begin(meter)
    assert meter.snapshot()["players"][0]["share"] == 0


def test_efficiency_uses_player_rounds_and_only_actual_will_spending():
    meter = Meter()
    begin(meter)
    meter.apply(Event("turn_phase", data={"zone": 99, "phase": "StartPlayerTurn"}))
    meter.apply(Event("turn_phase", data={"zone": 99, "phase": "StartPlayerTurn"}))
    meter.apply(Event("turn_phase", data={"zone": 99, "phase": "EnemyTurn"}))
    meter.apply(Event("turn_phase", data={"zone": 99, "phase": "StartPlayerTurn"}))
    meter.apply(
        Event("resource", data={"id": 1, "resource": "ManaPoints", "previous": 4, "value": 1})
    )
    meter.apply(
        Event("resource", data={"id": 1, "resource": "ManaPoints", "previous": 1, "value": 3})
    )
    meter.apply(parse_line(damage(90)))
    run = meter.current
    assert run is not None
    for totals in (run.support, run.encounters[-1].support):
        totals.add("healing", entry(1, "Mend", 30, {}))
        totals.add("taken", entry(1, "Enemy", 10, {}))
    snapshot = meter.snapshot()
    damage_row = snapshot["players"][0]
    healing_row = snapshot["metrics"]["healing"]["players"][0]
    taken_row = snapshot["metrics"]["taken"]["players"][0]
    assert (snapshot["rounds"], snapshot["will_spent"]) == (2, 3)
    assert (damage_row["per_round"], damage_row["per_will"]) == (45, 30)
    assert (healing_row["per_round"], healing_row["per_will"]) == (15, 10)
    assert (taken_row["per_round"], taken_row["per_will"]) == (5, None)


def test_resource_parser_keeps_both_values_needed_for_will_efficiency():
    event = parse_line(
        broadcast(
            "EventOnUnitResourceModified-WorldStateChangeModifyResource-"
            "UnitEntityHandle:(EntityHandle:4)-ResourceType:ManaPoints-PreviousValue:5-NewValue:2"
        )
    )
    assert event.data == {"id": 4, "resource": "ManaPoints", "previous": 5, "value": 2}
