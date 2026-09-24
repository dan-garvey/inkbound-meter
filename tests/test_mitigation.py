import json
from pathlib import Path

import pytest

from inkbound_meter.components import DamageContext, fix_to_int
from inkbound_meter.mitigation import MitigationContext
from inkbound_meter.parser import Event, parse_line

from .helpers import broadcast, damage
from .test_support import apply_log, incoming, player, record, setup_log, shield

ATTACK = "TuhningGuardian_AoESlam_Action"
ABILITY = "TuhningGuardian_AoESlam_AbilityData"


def mitigation_meter():
    text = setup_log().replace(
        "UnitData-Enemy (fixture)-Team:Enemy",
        "UnitData-MeleeInkling_Medium_ALL_T1_Unit (fixture)-Team:Enemy-IsResumingRun:False",
    )
    meter = apply_log(text)
    for entity, zone in ((9, 99), (2, 99), (1, -1)):
        meter.apply(Event("combat_zone", data={"id": entity, "zone": zone}))
    set_stat(meter, "SF1ScOK9", 20, entity=9)
    return meter


def set_stat(meter, key, value, entity=2):
    meter.apply(Event("stat", data={"id": entity, "stat": key, "value": value}))


def add_status(meter, effect, stacks=1, *, entity=2, source=2, instance=1):
    meter.apply(
        Event(
            "status",
            data={
                "source": source,
                "target": entity,
                "effect": effect,
                "stacks": stacks,
                "instance": instance,
            },
        )
    )


def hit(meter, amount, hp=50, action=ATTACK, ability=ABILITY):
    for line in incoming(
        amount, hp, action=action, AbilityData=f"AbilityData-{ability} (fixture)"
    ).splitlines():
        meter.apply(parse_line(line))
    return player(meter, "taken", 2)["mitigation"]


def credits(result):
    return {v["name"]: v["damage"] for v in result["components"]}


def supported_meter():
    meter = mitigation_meter()
    set_stat(meter, "3TCx0XPg", 20)
    add_status(meter, "Warden_Set_5_StatusEffect")
    set_stat(meter, "TWM45OpA", 2)
    add_status(meter, "Blur_StatusEffect", 2, instance=2)
    for line in (record(1, "EDclDmMI") + record(2, "KuFLDDDD") + shield(6, balance=6)).splitlines():
        meter.apply(parse_line(line))
    hit(meter, 14, hp=42)
    return meter


def test_percent_blur_then_shield_are_separate_and_provider_credit_is_preserved():
    meter = supported_meter()
    row = player(meter, "taken", 2)
    assert row["amount"] == 14
    assert credits(row) == {"Health lost": 8, "Absorbed": 6}
    mitigation = row["mitigation"]
    assert mitigation["prevented"] == 6 and mitigation["absorbed"] == 6
    assert credits(mitigation) == {"Warden set (5)": 4, "Blur": 2}
    assert mitigation["shield_providers"] == [{"name": "Healer", "amount": 6}]
    assert player(meter, "shielding", 1)["amount"] == 6
    assert credits(player(meter, "shielding", 1)) == {"Absorbed": 6}
    assert player(meter, "taken", 2, scope="encounter")["mitigation"] == mitigation
    # Later expiry/stat changes cannot rewrite the prevention of an earlier hit.
    set_stat(meter, "TWM45OpA", 0)
    add_status(meter, "Blur_StatusEffect", 0, instance=2)
    assert player(meter, "taken", 2)["mitigation"] == mitigation


@pytest.mark.parametrize(
    "attack,damage,expected",
    [
        (20, 14, {"Blur": 2, "Vestige: Efu’s Guise": 4}),
        (4, 0, {"Blur": 2, "Vestige: Efu’s Guise": 2}),
        (2, 0, {"Blur": 2}),
    ],
)
def test_blur_vestige_bonus_is_capped_by_damage_remaining(attack, damage, expected):
    meter = mitigation_meter()
    set_stat(meter, "SF1ScOK9", attack, entity=9)
    set_stat(meter, "TWM45OpA", 6)
    add_status(meter, "Blur_StatusEffect", 2)
    add_status(meter, "Vestige_Legendary_DoubleBlurNoShield_StatusEffect", instance=2)
    result = hit(meter, damage)
    assert result["matched_hits"] == 1 and result["unresolved_hits"] == 0
    assert credits(result) == expected
    assert result["prevented"] + damage == attack


def test_blur_bonus_requires_status_on_the_blur_provider():
    meter = mitigation_meter()
    set_stat(meter, "TWM45OpA", 6)
    add_status(meter, "Blur_StatusEffect", 2, source=1)
    add_status(meter, "Vestige_Legendary_DoubleBlurNoShield_StatusEffect", instance=2)
    assert credits(hit(meter, 14)) == {"Blur": 2, "Flat damage reduction": 4}


def test_indirect_dot_ignores_blur_and_reduces_each_stack_before_multiplying():
    meter = mitigation_meter()
    set_stat(meter, "TWM45OpA", 50)
    add_status(meter, "Blur_StatusEffect", 50)
    set_stat(meter, "3TCx0XPg", 20)
    add_status(meter, "Warden_Set_5_StatusEffect", instance=2)
    add_status(meter, "Burn_StatusEffect", 3, source=9, instance=3)
    event = parse_line(
        damage(
            24,
            source=9,
            target=2,
            action="Burn_Damage_Action",
            TargetUnitTeam="Friendly",
            AbilityData="(none)",
            StatusEffectData="StatusEffectData-Burn_StatusEffect (fixture)",
        )
    )
    meter.apply(event)
    result = player(meter, "taken", 2)["mitigation"]
    assert result["matched_hits"] == 1
    assert credits(result) == {"Warden set (5)": 6}


@pytest.mark.parametrize(
    "second_zone,easy,damage", [(99, False, 24), (-1, False, 18), (99, True, 19)]
)
def test_direct_target_scaling_counts_only_friendly_units_in_the_same_zone(
    second_zone, easy, damage
):
    meter = mitigation_meter()
    meter.apply(Event("combat_zone", data={"id": 1, "zone": second_zone}))
    if easy:
        add_status(meter, "ChallengeRunData_EasyMode_StatusEffect", instance=3)
    set_stat(meter, "TWM45OpA", 2)
    add_status(meter, "Blur_StatusEffect", 2)
    result = hit(
        meter, damage, action="FigmentStab_Damage_Action", ability="FigmentStab_AbilityData"
    )
    assert result["matched_hits"] == 1 and credits(result) == {"Blur": 2}


def test_missing_history_mismatch_and_unknown_build_do_not_invent_prevention():
    for problem in ("initial", "mismatch", "build", "zone"):
        meter = mitigation_meter()
        set_stat(meter, "TWM45OpA", 5)
        add_status(meter, "Blur_StatusEffect", 5)
        if problem == "initial":
            meter.damage_context.fresh.remove(2)
        elif problem == "build":
            meter.damage_context.build = 99999
        elif problem == "zone":
            meter.support_context.mitigation.zones.clear()
        result = hit(
            meter,
            0 if problem == "mismatch" else 15,
            action="FigmentStab_Damage_Action",
            ability="FigmentStab_AbilityData",
        )
        assert result["prevented"] == 0 and result["matched_hits"] == 0
        assert result["unresolved_hits"] == 1 and result["reasons"]


def test_unidentified_item_and_capped_or_offset_stat_use_generic_damage_reduction():
    meter = mitigation_meter()
    set_stat(meter, "3TCx0XPg", 20)
    assert credits(hit(meter, 16)) == {"Damage reduction": 4}
    meter = mitigation_meter()
    set_stat(meter, "3TCx0XPg", 10)
    add_status(meter, "Warden_Set_5_StatusEffect")
    assert credits(hit(meter, 18)) == {"Damage reduction": 2}


def test_named_vestige_marker_and_constant_set_do_not_scale_by_status_stacks():
    meter = mitigation_meter()
    set_stat(meter, "3TCx0XPg", 30)
    add_status(meter, "Warden_Set_5_StatusEffect", 5)
    add_status(meter, "VestigeAll_Uncommon_Teleport_StatusEffect", instance=2)
    assert credits(hit(meter, 14)) == {"Warden set (5)": 4, "Vestige: Port-a-Hole": 2}


def test_percent_stages_keep_remainder_carry_before_flat_reduction():
    meter = mitigation_meter()
    set_stat(meter, "SF1ScOK9", 7, entity=9)
    set_stat(meter, "3TCx0XPg", 20)
    add_status(meter, "Warden_Set_5_StatusEffect")
    set_stat(meter, "pq6hlsOe", 25)
    set_stat(meter, "TWM45OpA", 2)
    add_status(meter, "Blur_StatusEffect", 2, instance=2)
    # 7 * .8 * .75 = 4.2, truncated to 4, then 2 Blur: 2 damage.
    result = hit(meter, 2)
    assert credits(result) == {"Warden set (5)": 2, "Uncapped damage reduction": 1, "Blur": 2}


def test_literal_damage_bypasses_blur_and_percent_defenses():
    meter = mitigation_meter()
    set_stat(meter, "TWM45OpA", 50)
    set_stat(meter, "3TCx0XPg", 50)
    result = hit(meter, 2, action="ChannelerFire_DamageReflect_Action", ability="(none)")
    assert result["matched_hits"] == 1 and credits(result) == {}


def test_enemy_scaling_uses_round_to_even_after_fixed_point_division():
    assert fix_to_int(4096 * 2 + 2048) == 2
    assert fix_to_int(4096 * 3 + 2048) == 4
    meter = mitigation_meter()
    set_stat(meter, "SF1ScOK9", 10, entity=9)
    set_stat(meter, "TWM45OpA", 2)
    add_status(meter, "Blur_StatusEffect", 2)
    assert credits(
        hit(meter, 6, action="Razorshade_Dash_Action", ability="Razorshade_Dash_AbilityData")
    ) == {"Blur": 2}


def test_combat_zone_parser_and_disconnect_remove_stale_party_size():
    state = MitigationContext()
    for name in (
        "EventOnUnitEnterCombat",
        "EventOnUnitEnterCombatWorldSync",
        "EventOnUnitExitCombat",
    ):
        event = parse_line(
            broadcast(
                f"{name}-WorldStateChangeUnitEnterCombat-UnitEntityHandle:(EntityHandle:2)-"
                "CombatZoneEntityHandle:(EntityHandle:99)-CurrentTurnPhase:INITIAL-Team:Friendly"
            )
        )
        state.observe(event)
        assert state.zones[2] == (-1 if name.endswith("ExitCombat") else 99)
    state.observe(Event("connection", data={"connected": False}))
    assert state.zones == {} and state.teams == {}


def test_recorded_incoming_hits_match_independent_expected_prevention():
    fixtures = json.loads((Path(__file__).parent / "fixtures/mitigation_hits.json").read_text())
    for fixture in fixtures:
        context, mitigation = DamageContext(), MitigationContext()
        for value in fixture["events"]:
            event = Event(**value)
            context.apply(event)
            mitigation.observe(event)
        result = mitigation.explain(Event("damage", data=fixture["hit"]), context)
        assert result == fixture["expected"], fixture["hit"]


def test_v6_history_backfills_party_zones_without_changing_saved_hits(tmp_path):
    from inkbound_meter.service import Session, replay

    text = setup_log().replace(
        "UnitData-Enemy (fixture)-Team:Enemy",
        "UnitData-MeleeInkling_Medium_ALL_T1_Unit (fixture)-Team:Enemy-"
        "IsResumingRun:False-CombatZoneHandle:(EntityHandle:99)",
    )
    text += broadcast(
        "EventOnUnitEnterCombat-UnitEntityHandle:(EntityHandle:2)-"
        "CombatZoneEntityHandle:(EntityHandle:99)-Team:Friendly"
    )
    text += broadcast(
        "EventOnUnitStatModified-UnitEntityHandle:(EntityHandle:9)-"
        "StatDataGuid:SF1ScOK9-NewValue:20"
    )
    text += broadcast(
        "EventOnUnitStatModified-UnitEntityHandle:(EntityHandle:2)-StatDataGuid:TWM45OpA-NewValue:2"
    )
    text += incoming(
        18,
        32,
        action="FigmentStab_Damage_Action",
        AbilityData="AbilityData-FigmentStab_AbilityData (fixture)",
    )
    log, database = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    log.write_text(text)
    session = Session(log, database)
    session.poll()
    saved = [
        tuple(r)
        for r in session.store.db.execute(
            "SELECT sequence,byte_offset,data FROM events WHERE kind='damage'"
        )
    ]
    # Recreate the version-6 journal shape, retaining the verified file cursor.
    with session.store.db:
        session.store.db.execute("DELETE FROM events WHERE kind='combat_zone'")
        for row in session.store.db.execute("SELECT sequence,data FROM events").fetchall():
            data = json.loads(row[1])
            if "zone" in data and "unit_data" in data:
                data.pop("zone")
                session.store.db.execute(
                    "UPDATE events SET data=? WHERE sequence=?", (json.dumps(data), row[0])
                )
        session.store.db.execute("UPDATE streams SET parser_version=6")
    session.close()
    session = Session(log, database)
    assert player(session.meter, "taken", 2)["mitigation"]["unresolved_hits"] == 1
    assert session.poll().rebuild
    assert session.meter.report() == replay(log).report()
    assert player(session.meter, "taken", 2)["mitigation"]["prevented"] == 2
    assert [
        tuple(r)
        for r in session.store.db.execute(
            "SELECT sequence,byte_offset,data FROM events WHERE kind='damage'"
        )
    ] == saved
    session.close()
    session = Session(log, database)
    assert not session.poll().rebuild
    assert session.meter.report() == replay(log).report()
    session.close()
