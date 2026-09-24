"""Provider and overheal regressions for recorded multiplayer healing paths."""

import json
from pathlib import Path

import pytest

from inkbound_meter.model import Meter
from inkbound_meter.parser import Event
from inkbound_meter.support import log_time, support_catalog

from .test_support import apply_log, components, player, setup_log

RESTORE = "Restoration_AbilityData"
HEAL = "Restoration_Heal_Action"
MEND = "Restoration_Legendary_Mend_AbilityData"
MEND_HEAL = "Restoration_Legendary_Mend_Heal_Action"
ALLY = "VestigeAll_Epic_AllyHeal2_AbilityData"
ALLY_HEAL = "VestigeAll_Epic_AllyHeal_Heal_Action"
ALLY_STATUS = "VestigeAll_Epic_AllyHeal_StatusEffect"


def cast(meter, entity=1, ability=RESTORE, timestamp="0T12:00:00 97"):
    meter.apply(
        Event(
            "position",
            timestamp,
            {
                "id": entity,
                "ability": ability,
                "position": [0, 0],
            },
        )
    )


def healed(
    meter,
    amount=1,
    hp=50,
    *,
    target=2,
    action=HEAL,
    ability=RESTORE,
    effect=None,
    max_hp=50,
    timestamp="0T12:00:00 03",
    combat=True,
):
    meter.apply(
        Event(
            "heal",
            timestamp,
            {
                "target": target,
                "target_team": "Friendly",
                "amount": amount,
                "hp": hp,
                "max_hp": max_hp,
                "shield": 0,
                "action": action,
                "ability": ability,
                "effect": effect,
                "in_combat": combat,
            },
        )
    )


def status(meter, effect, stacks=1, target=1, source=1, instance=1):
    meter.apply(
        Event(
            "status",
            "0T12:00:00 01",
            {
                "effect": effect,
                "source": source,
                "target": target,
                "instance": instance,
                "stacks": stacks,
            },
        )
    )


def stat(meter, key, value, entity=1):
    meter.apply(Event("stat", data={"id": entity, "stat": key, "value": value}))


def test_restoration_credits_its_logged_caster_across_frame_counter_wrap():
    meter = apply_log(setup_log())
    cast(meter)
    # An unrelated actor's action must not steal the Restoration provider.
    cast(meter, entity=2, ability="Bonk_AbilityData", timestamp="0T12:00:00 99")
    healed(meter)
    row = player(meter, "healing", 1)
    assert row["amount"] == 4
    assert components(row) == {"HP restored": 1, "Overheal": 3}
    assert row["recipients"] == [{"name": "Tank", "amount": 4}]
    assert player(meter, "healing", 2)["amount"] == 0
    assert log_time("1T01:45:10 97") == log_time("1T01:45:10 03")


def test_mend_adds_source_healing_stat_and_modifiers_and_keeps_received_healing_separate():
    meter = apply_log(setup_log())
    stat(meter, "f3Yv3WIn", 5)
    stat(meter, "pROS9yE0", 100)
    stat(meter, "1SDlVYuI", 25, entity=2)
    cast(meter, ability=MEND)
    healed(meter, amount=5, action=MEND_HEAL, ability=MEND)
    assert components(player(meter, "healing", 1)) == {"HP restored": 5, "Overheal": 10}
    assert player(meter, "healing", 2)["amount"] == 0


def test_ally_vestige_heals_scale_only_with_the_casting_players_copies():
    meter = apply_log(setup_log())
    status(meter, ALLY_STATUS, 2)
    status(meter, ALLY_STATUS, 7, target=2, source=2, instance=2)
    cast(meter, ability=ALLY)
    healed(meter, amount=1, action=ALLY_HEAL, ability=ALLY, effect=ALLY_STATUS)
    assert components(player(meter, "healing", 1)) == {"HP restored": 1, "Overheal": 5}
    assert player(meter, "healing", 2)["amount"] == 0


def test_sapper_self_heal_and_overheal_do_not_need_the_already_removed_thread_status():
    meter = apply_log(setup_log())
    for _ in range(2):
        healed(
            meter,
            amount=0,
            target=1,
            action="ThreadUpgrade_Epic_Sapper_OnDetach_Action",
            ability=None,
            effect="Threaded_StatusEffect",
        )
    assert components(player(meter, "healing", 1)) == {"Overheal": 2}
    assert player(meter, "healing", 1)["events"] == 2


def test_end_of_combat_vestige_uses_observed_stacks_and_belongs_to_the_run():
    meter = apply_log(setup_log())
    effect = "VestigeAll_Rare_APOnCombatEnd_StatusEffect"
    status(meter, effect, 3)
    healed(
        meter,
        2,
        target=1,
        action="VestigeAll_Rare_APOnCombatEnd_Action",
        ability=None,
        effect=effect,
        combat=False,
    )
    assert components(player(meter, "healing", 1)) == {"HP restored": 2, "Overheal": 16}
    assert player(meter, "healing", 1, scope="encounter")["amount"] == 0


@pytest.mark.parametrize(
    "problem",
    ["missing", "expired", "wrong_action", "wrong_ability", "turn", "removed", "unknown_build"],
)
def test_unrelated_stale_or_missing_casts_never_guess_the_provider(problem):
    meter = apply_log(setup_log())
    if problem != "missing":
        cast(meter)
    options = {}
    if problem == "expired":
        options["timestamp"] = "0T12:00:03 01"
    elif problem == "wrong_action":
        options["action"] = "Restoration_Legendary_Mend_Heal_Action"
    elif problem == "wrong_ability":
        options["ability"] = "UnknownAbility"
    elif problem == "turn":
        meter.apply(Event("turn_phase", data={"zone": 99, "phase": "EndPlayerTurn"}))
    elif problem == "removed":
        meter.apply(Event("unit_removed", data={"id": 1}))
    elif problem == "unknown_build":
        meter.damage_context.build = 99999
    healed(meter, **options)
    view = meter.snapshot()["metrics"]["healing"]
    assert view["party_total"] == 0 and view["unattributed"] == 1


def test_heal_budget_prevents_a_cast_being_reused_for_the_same_target():
    meter = apply_log(setup_log())
    cast(meter)
    healed(meter)
    healed(meter)
    assert player(meter, "healing", 1)["amount"] == 4
    assert meter.snapshot()["metrics"]["healing"]["unattributed"] == 1
    # The same area cast can legitimately heal another recipient once.
    healed(meter, target=1)
    assert player(meter, "healing", 1)["amount"] == 8
    cast(meter, timestamp="0T12:00:01 10")
    healed(meter, timestamp="0T12:00:01 12")
    assert player(meter, "healing", 1)["amount"] == 12


def test_overlapping_same_ability_casts_remain_ambiguous_after_one_expires():
    meter = apply_log(setup_log())
    cast(meter, timestamp="0T12:00:00 10")
    cast(meter, entity=2, timestamp="0T12:00:01 10")
    healed(meter, timestamp="0T12:00:01 20")
    # We cannot know which candidate supplied the first heal. Expiring caster 1
    # must not make caster 2 available to claim that same cast's credit later.
    healed(meter, timestamp="0T12:00:03 10")
    view = meter.snapshot()["metrics"]["healing"]
    assert view["party_total"] == 0 and view["unattributed"] == 2


def test_record_pair_can_disambiguate_overlapping_casts_and_consumes_the_cast():
    meter = apply_log(setup_log())
    cast(meter)
    cast(meter, entity=2)
    for entity, record in ((1, "hkIlI6PO"), (2, "sI7wIL3j")):
        meter.apply(Event("support_record", "0T12:00:00 03", {"id": entity, "record": record}))
    healed(meter)
    assert player(meter, "healing", 1)["amount"] == 4
    assert meter.support_context.heal_casts[(1, RESTORE)]["used"][(HEAL, 2)] == 1


def test_partial_capture_still_credits_cast_but_does_not_invent_capped_overheal():
    meter = apply_log(setup_log())
    meter.damage_context.fresh.clear()
    cast(meter)
    healed(meter)
    row = player(meter, "healing", 1)
    assert components(row) == {"HP restored": 1}
    assert row["unknown_overheal"] == 1
    cast(meter, timestamp="0T12:00:01 01")
    healed(meter, amount=4, hp=40, timestamp="0T12:00:01 03")
    row = player(meter, "healing", 1)
    assert row["amount"] == 5 and row["unknown_overheal"] == 1


def test_percent_max_health_heal_uses_games_rounding_instead_of_truncation():
    meter = apply_log(setup_log())
    for entity, record in ((1, "hkIlI6PO"), (2, "sI7wIL3j")):
        meter.apply(Event("support_record", "0T12:00:00 03", {"id": entity, "record": record}))
    healed(meter, action="Heal25_Action", ability=None, hp=51, max_hp=51)
    # 25% of 51 rounds to 13 before healing modifiers and the missing-HP cap.
    assert components(player(meter, "healing", 1)) == {"HP restored": 1, "Overheal": 12}


def test_identical_branches_and_exact_direct_ability_actions_are_mapped():
    catalog = support_catalog()
    assert catalog["actions"][HEAL]["amount"]["args"][0] == 4
    assert catalog["actions"][MEND_HEAL]["amount"]["args"][0] == 5
    assert (
        catalog["actions"]["ThreadUpgrade_Epic_Sapper_OnDetach_Action"]["owner"] == "status_source"
    )
    assert catalog["abilities"][RESTORE][HEAL] == 1
    assert MEND_HEAL not in catalog["abilities"][RESTORE]


def test_recorded_healing_regressions_credit_providers_and_overheal():
    # Minimized recorded broadcasts with anonymous IDs/names and only the
    # observed stats/statuses required by each inspected healing formula.
    fixtures = json.loads((Path(__file__).parent / "fixtures/healing_events.json").read_text())
    for fixture in fixtures:
        meter = Meter()
        for value in fixture["events"]:
            meter.apply(Event(**value))
        values = meter.snapshot(scope="run")["metrics"]["healing"]
        assert values["party_total"] == fixture["expected_total"], fixture["name"]
        for expected in fixture["expected_players"]:
            row = player(meter, "healing", expected["id"])
            assert components(row) == expected["components"], fixture["name"]
