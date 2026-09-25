import json
from pathlib import Path

import pytest

from inkbound_meter.components import DamageContext
from inkbound_meter.model import Meter
from inkbound_meter.parser import Event, parse_line
from inkbound_meter.service import replay

from .helpers import broadcast, damage

FROST = "FrostBite_Damage_StatusEffect_Action"
RECORDED_HITS = json.loads(
    (Path(__file__).parent / "fixtures/expanded_damage_hits.json").read_text()
)


def context():
    ctx = DamageContext()
    for event in [
        Event("game_build", data={"build": 24243}),
        Event("run_create"),
        Event("connection", data={"connected": True}),
        Event(
            "player",
            data={"id": 1, "unit_data": "BasePlayerData", "resuming": False, "class_id": "C03"},
        ),
        Event("unit", data={"id": 99, "unit_data": "BasePlayerData", "resuming": False}),
    ]:
        ctx.apply(event)
    return ctx


def hit(ctx, amount, action=FROST, **fields):
    fields.setdefault("AbilityData", "(none)")
    return ctx.explain(parse_line(damage(amount, action=action, **fields)))


def stat(ctx, key, value, entity=1):
    ctx.apply(Event("stat", data={"id": entity, "stat": key, "value": value}))


def status(ctx, effect, stacks, target=1, source=1, instance=10):
    ctx.apply(
        Event(
            "status",
            data={
                "effect": effect,
                "stacks": stacks,
                "target": target,
                "source": source,
                "instance": instance,
            },
        )
    )


def credits(result):
    return {v["name"]: v["damage"] for v in result["components"]}


def test_frostbite_flat_bonus_and_additive_magic_omni_are_not_compounded():
    ctx = context()
    stat(ctx, "GxdpGRgP", 20)
    stat(ctx, "kpnF2coo", 28)
    stat(ctx, "K3G3pgjn", 15)
    # 70 * 1.43 = 100.10, rounded up to 101. The unused DamageTo stat
    # must not multiply an indirect Frostbite proc.
    stat(ctx, "hfVI0nDK", 200)
    result = hit(ctx, 101)
    assert result["status"] == "matched"
    assert credits(result) == {
        "Base effect": 50,
        "Frostbite damage": 20,
        "Magic damage": 10.5,
        "Omni damage": 19.6,
        "Rounding": 0.9,
    }
    assert sum(result["credits"].values()) == 10100
    stat(ctx, "kpnF2coo", 31)
    assert hit(ctx, 103)["status"] == "matched"
    assert result["inputs"]["Omni damage"] == 28  # Historical snapshot, not current stats.


def test_frostbite_burn_crossover_depends_on_caster_status():
    ctx = context()
    stat(ctx, "GxdpGRgP", 50)
    stat(ctx, "vBPDze3C", 10)
    stat(ctx, "kpnF2coo", 58)
    stat(ctx, "K3G3pgjn", 25)
    assert hit(ctx, 183)["status"] == "matched"
    status(ctx, "VestigeAll_Rare_InspirationOfFlame_StatusEffect", 1)
    result = hit(ctx, 202)
    assert result["status"] == "matched"
    assert credits(result)["Burn crossover"] == 10
    status(ctx, "VestigeAll_Rare_InspirationOfFlame_StatusEffect", 0)
    assert hit(ctx, 183)["status"] == "matched"


def test_burn_rounds_per_stack_and_uses_only_this_casters_instance():
    ctx = context()
    stat(ctx, "K3G3pgjn", 15)
    status(ctx, "Burn_StatusEffect", 3, target=99)
    status(ctx, "Burn_StatusEffect", 9, target=99, source=2, instance=11)
    # ceil(10 * 1.15) * 3 = 36, not ceil(30 * 1.15) = 35.
    result = hit(ctx, 36, "Burn_Damage_Action")
    assert result["status"] == "matched" and result["stacks"] == 3
    assert credits(result)["Rounding"] == 1.5
    status(ctx, "Burn_StatusEffect", 2, target=99, instance=12)
    assert "ambiguous" in hit(ctx, 36, "Burn_Damage_Action")["reason"]


def test_divine_touch_smite_proc_uses_its_holders_smite_damage():
    ctx = context()
    stat(ctx, "l6KzwBB0", 60)
    # Inspected graph: 50 + Smite damage. The attached status is on the caster;
    # the action's context target receives its own, separate damage broadcast.
    result = hit(ctx, 110, "Smite_Damage_StatusEffect_Action")
    assert result["status"] == "matched"
    assert credits(result) == {"Base effect": 50, "Smite damage": 60}


def test_legendary_vestige_smite_again_uses_its_owners_smite_and_magic_tags():
    ctx = context()
    stat(ctx, "l6KzwBB0", 60)
    # Inspected equipment graph: 50 + the owner's Smite stat. Unlike standard
    # Smite it carries Magic, but not the Smite action tag.
    result = hit(ctx, 110, "VestigeAll_Legendary_SmiteAgain_Action")
    assert result["status"] == "matched"
    assert credits(result) == {"Base effect": 50, "Smite damage": 60}


def test_legendary_vestige_smite_again_explains_a_near_death_health_cap():
    ctx = context()
    stat(ctx, "l6KzwBB0", 200)
    status(ctx, "CanBeNearDeath_StatusEffect", 1, target=99, source=99)
    ctx.apply(Event("damage_health", data={"target": 99, "source": 1, "amount": 45, "hp": 1}))
    result = hit(ctx, 45, "VestigeAll_Legendary_SmiteAgain_Action")
    assert result["status"] == "matched"
    assert credits(result) == {
        "Base effect": 50,
        "Smite damage": 200,
        "Near-death limit": -205,
    }


def test_resuming_party_members_use_their_inspected_class_baseline_when_log_has_run_start():
    ctx = DamageContext()
    for event in [
        Event("game_build", data={"build": 24243}),
        Event("run_create"),
        Event("connection", data={"connected": True}),
        Event(
            "player",
            data={"id": 1, "unit_data": "BasePlayerData", "resuming": True, "class_id": "C05"},
        ),
        Event("unit", data={"id": 99, "unit_data": "BasePlayerData", "resuming": True}),
    ]:
        ctx.apply(event)
    assert hit(ctx, 50)["status"] == "matched"


def test_current_shield_from_health_state_is_available_to_damage_formula_inputs():
    ctx = context()
    ctx.apply(Event("health", data={"id": 1, "hp": 50, "shield": 37, "max_hp": 50}))
    assert ctx.stats[1]["A3xbQ1as"] == 37
    ctx.apply(
        Event(
            "shield",
            data={"target": 1, "target_team": "Friendly", "amount": 8, "shield": 45},
        )
    )
    assert ctx.stats[1]["A3xbQ1as"] == 45


def test_legendary_headbutt_uses_the_casters_current_shield():
    ctx = context()
    stat(ctx, "A3xbQ1as", 5)
    result = hit(ctx, 55, "ShieldBashUpgrade_Legendary_Headbutt_Damage_Action")
    assert result["status"] == "matched"
    assert credits(result) == {"Base effect": 50, "Current Energy Shield": 5}


def test_increases_round_up_then_reductions_round_down():
    ctx = context()
    stat(ctx, "K3G3pgjn", 1)
    stat(ctx, "pq6hlsOe", 25, entity=99)
    result = hit(ctx, 38)  # ceil(50.5) * .75 = 38.25, truncated to 38.
    assert result["status"] == "matched"
    assert credits(result)["Uncapped damage reduction"] == -12.75
    assert credits(result)["Rounding"] == 0.25
    assert sum(result["credits"].values()) == 3800


def test_unknown_build_partial_history_and_unsupported_formula_are_explicit():
    ctx = context()
    ctx.build = 99999
    assert "build" in hit(ctx, 50)["reason"]
    ctx.build = 24243
    ctx.full_history = False
    assert "Initial" in hit(ctx, 50)["reason"]
    ctx.full_history = True
    assert "not mapped" in hit(ctx, 50, "FutureDamage_Action")["reason"]
    assert "Zero hit" in hit(ctx, 0)["reason"]
    ctx.apply(Event("connection", data={"connected": True}))
    assert not ctx.full_history and not ctx.stats and not ctx.statuses


def test_mismatched_hit_never_enters_component_totals_or_changes_damage():
    from inkbound_meter.components import Breakdown

    ctx = context()
    breakdown = Breakdown()
    breakdown.add(hit(ctx, 50), 50)
    mismatch = hit(ctx, 49)
    assert mismatch["status"] == "mismatch" and mismatch["difference"] == -1
    breakdown.add(mismatch, 49)
    view = breakdown.view()
    assert view["matched_damage"] == 50 and view["unresolved_damage"] == 49
    assert view["matched_hits"] == 1 and view["hits"] == 2
    assert view["components"] == [{"name": "Base effect", "damage": 50}]


def test_real_frostbite_run_reconstructs_75_hits_and_keeps_phase_hit_unresolved():
    meter = replay(Path(__file__).parent / "fixtures/frostbite_components.txt")
    source = meter.snapshot(scope="run")["players"][0]["sources"][0]
    b = source["breakdown"]
    assert meter.parse_errors == 0
    assert source["damage"] == 21798
    assert (b["hits"], b["matched_hits"], b["unresolved_damage"]) == (76, 75, 120)
    assert b["matched_damage"] + b["unresolved_damage"] == source["damage"]
    assert sum(round(c["damage"] * 100) for c in b["components"]) == b["matched_damage"] * 100
    assert b["latest"]["predicted"] == b["latest"]["observed"] == 387


def test_new_parser_uses_only_broadcasts_and_keeps_negative_stat_values():
    line = broadcast(
        "EventOnUnitStatModified-WorldStateChangeModifyStat-"
        "UnitEntityHandle:(EntityHandle:1)-StatName:(custom stat)-"
        "StatDataGuid:GxdpGRgP-NewValue:-10"
    )
    assert parse_line(line).data == {"id": 1, "stat": "GxdpGRgP", "value": -10}
    assert parse_line(line.replace("broadcasting", "handling")) is None


def test_component_failure_cannot_stop_damage_collection(monkeypatch):
    from .helpers import begin

    meter = Meter()
    begin(meter)

    def broken(_):
        raise ValueError("unmapped shape")

    monkeypatch.setattr(meter.damage_context, "explain", broken)
    meter.apply(parse_line(damage(706)))
    assert meter.snapshot()["party_damage"] == 706
    assert meter.snapshot()["players"][0]["sources"][0]["breakdown"]["unresolved_damage"] == 706


def test_binding_uses_target_status_bonus_and_critical_multiplier():
    ctx = context()
    stat(ctx, "kpnF2coo", 28)
    stat(ctx, "K3G3pgjn", 15)
    stat(ctx, "hfVI0nDK", 25)
    status(ctx, "Frostbite_StatusEffect", 1, target=99)
    result = hit(
        ctx,
        242,
        "PsychicPulse_Damage_ActionData",
        AbilityData="AbilityData-PsychicPulse_AbilityData (fixture)",
        IsCriticalHit="True",
    )
    assert result["status"] == "matched"
    assert credits(result)["Critical hit"] > 0
    assert sum(result["credits"].values()) == 24200


def test_full_charge_scaling_uses_status_at_each_hit():
    ctx = context()
    stat(ctx, "K3G3pgjn", 15)
    status(ctx, "FullCharge_StatusEffect", 1)
    result = hit(ctx, 259, "SpiritBomb_Damage_Action")
    assert result["status"] == "matched"
    assert credits(result)["Base effect"] == 125
    assert credits(result)["Full charge"] == 100
    status(ctx, "FullCharge_StatusEffect", 0)
    assert hit(ctx, 144, "SpiritBomb_Damage_Action")["status"] == "matched"


@pytest.mark.parametrize("record", RECORDED_HITS, ids=lambda r: r["action"])
def test_additional_formulas_match_recorded_hits_and_preserve_each_component(record):
    ctx = DamageContext()
    facts = record["context"]
    ctx.build, ctx.full_history = facts["build"], facts["full_history"]
    ctx.fresh = set(facts["fresh"])
    for field in ("stats", "units", "positions", "cast_abilities"):
        getattr(ctx, field).update({int(k): v for k, v in facts[field].items()})
    ctx.statuses = {(s["target"], s["instance"]): s for s in facts["statuses"]}
    event = Event(**record["event"])
    result = ctx.explain(event)
    assert result["status"] == "matched", result
    assert result["components"] == record["expected_components"]
    assert sum(result["credits"].values()) == event.data["amount"] * 100
    if "RecklessLunge" in record["action"]:
        ctx.cast_abilities.clear()
        assert ctx.explain(event)["reason"] == "Positions for this cast are missing"


def test_throw_double_cast_is_two_broadcasts_not_double_damage_per_hit():
    from inkbound_meter.components import Breakdown

    ctx = context()
    status(ctx, "ThrowUpgrade_Epic_Twice_StatusEffect", 1)
    stat(ctx, "G7Yzfw8x", 10)
    stat(ctx, "zO4KuGPK", 25)
    breakdown = Breakdown()
    for _ in range(2):
        result = hit(ctx, 75, "Throw_Damage_Action")  # (50 + 10) * 1.25, per strike.
        assert result["status"] == "matched"
        breakdown.add(result, 75)
    assert breakdown.view()["matched_damage"] == 150
    assert breakdown.view()["matched_hits"] == 2


def test_chi_eruption_combo_requires_matching_caster_and_preserves_conditional_scaling():
    ctx = context()
    stat(ctx, "gcTtmbDJ", 20)
    stat(ctx, "8ctoCY21", 10)
    stat(ctx, "9BtTsxBV", 15)
    status(ctx, "ChiEruption_Rare_ComboBonus_StatusEffect", 2)
    status(ctx, "C08_Combo_StatusEffect", 1, source=2, instance=11)
    assert hit(ctx, 145, "ChiEruption_PhysicalDamage_Action")["status"] == "matched"
    status(ctx, "C08_Combo_StatusEffect", 1, instance=12)
    result = hit(ctx, 175, "ChiEruption_PhysicalDamage_Action")
    assert result["status"] == "matched"
    assert credits(result)["Combo bonus"] == 30


def test_cleave_disconnected_bleed_branch_does_not_add_damage():
    ctx = context()
    stat(ctx, "h2hESONJ", 40)
    status(ctx, "CleaveUpgrade_Epic_Heal_StatusEffect", 1)
    status(ctx, "Bleed_StatusEffect", 12, target=99, instance=11)
    assert hit(ctx, 240, "Cleave_Damage_Action")["status"] == "matched"


def test_spiked_uses_holders_stats_and_single_status_instance_even_when_given_by_ally():
    ctx = context()
    stat(ctx, "3tlEcxSf", 5)
    stat(ctx, "zO4KuGPK", 10)
    status(ctx, "Spiked_StatusEffect", 3, source=2)
    result = hit(ctx, 51, "Spiked_Action")  # ceil((10 + 5) * 1.1) * 3.
    assert result["status"] == "matched" and result["stacks"] == 3
    assert credits(result)["Rounding"] == 1.5
    status(ctx, "Spiked_StatusEffect", 4, source=3, instance=11)
    assert "ambiguous" in hit(ctx, 51, "Spiked_Action")["reason"]


def test_distance_uses_logged_fixed_point_positions_and_requires_current_cast():
    ctx = context()
    action = "RunicStrikeUpgrade_Legendary_RecklessLunge_PD_Distance_Action"
    ability = "RunicStrikeUpgrade_Legendary_RecklessLunge_AbilityData"
    field = f"AbilityData-{ability} (fixture)"
    ctx.apply(Event("position", data={"id": 1, "position": [-4096, -8192], "ability": ability}))
    ctx.apply(Event("position", data={"id": 99, "position": [8192, 8192]}))
    result = hit(ctx, 240, action, AbilityData=field)  # 115 + floor(5) * 25.
    assert result["status"] == "matched"
    assert credits(result)["Distance bonus"] == 125
    assert result["inputs"]["Distance (whole units)"] == 5
    ctx.apply(Event("position", data={"id": 99, "position": [8192, 8191]}))
    assert hit(ctx, 215, action, AbilityData=field)["status"] == "matched"
    ctx.apply(Event("turn_phase", data={"zone": 1, "phase": "EnemyTurn"}))
    assert "Positions" in hit(ctx, 215, action, AbilityData=field)["reason"]


def test_whirlwind_scaled_rings_use_the_target_hitbox_and_current_cast_position():
    ctx = context()
    ability = "Whirlwind_AbilityData"
    field = f"AbilityData-{ability} (fixture)"
    ctx.apply(Event("position", data={"id": 1, "position": [0, 0], "ability": ability}))
    ctx.apply(Event("unit", data={"id": 99, "position": [0, 2 * 4096], "hitbox_radius": 4096}))

    near = hit(ctx, 60, "Whirlwind_Action", AbilityData=field)
    assert near["status"] == "matched"
    assert near["inputs"]["Scaled range ring"] == 1
    assert "Scaled range" not in credits(near)

    # At four units to the target edge, Whirlwind crosses its 80% custom edge
    # into its 200% ring. The radius is part of the game's edge calculation.
    ctx.apply(Event("position", data={"id": 99, "position": [0, 3 * 4096]}))
    outer = hit(ctx, 120, "Whirlwind_Action", AbilityData=field)
    assert outer["status"] == "matched"
    assert outer["inputs"]["Scaled range ring"] == 0
    assert outer["inputs"]["Distance to target edge"] == 3
    assert credits(outer)["Scaled range"] == 60

    ctx.apply(Event("turn_phase", data={"zone": 1, "phase": "EnemyTurn"}))
    # Partial history can still prove an unscaled hit, but must not invent the
    # boosted ring without current positions and the target hitbox radius.
    assert hit(ctx, 60, "Whirlwind_Action", AbilityData=field)["status"] == "matched"
    assert hit(ctx, 120, "Whirlwind_Action", AbilityData=field)["status"] == "mismatch"


def test_whirlwind_keeps_its_range_scaling_through_a_same_tick_triggered_cast():
    ctx = context()
    ability = "Whirlwind_AbilityData"
    field = f"AbilityData-{ability} (fixture)"
    ctx.apply(Event("position", "tick", {"id": 1, "position": [0, 0], "ability": ability}))
    ctx.apply(Event("unit", data={"id": 99, "position": [0, 3 * 4096], "hitbox_radius": 4096}))
    # A triggered effect publishes another ability record before the damage
    # broadcast. The broadcast still identifies Whirlwind as the ability whose
    # scaled range needs to be reconstructed.
    ctx.apply(
        Event(
            "position",
            "tick",
            {
                "id": 1,
                "position": [0, 0],
                "ability": "VestigeAll_Rare_DamageOnShield_AbilityData",
            },
        )
    )
    event = parse_line(damage(120, action="Whirlwind_Action", AbilityData=field))
    result = ctx.explain(Event(event.kind, "tick", event.data))
    assert result["status"] == "matched"
    assert credits(result)["Scaled range"] == 60


@pytest.mark.parametrize(
    "body",
    [
        "EventOnUnitMoved-UnitEntityHandle-(EntityHandle:1)-EndPosition:(-1.000244, 2.000244)",
        "EventOnUnitTeleported-UnitHandle-(EntityHandle:1)-MoveToPosition:(-1.000244, 2.000244)",
        "EventOnUnitPlayedAbility-UnitEntityHandle:(EntityHandle:1)-"
        "FromWorldPosition:(-1.000244, 2.000244)-AbilityData:AbilityData-Test (fixture)",
    ],
)
def test_position_parser_recovers_fixed_point_coordinates_only_from_broadcasts(body):
    line = broadcast(body)
    event = parse_line(line)
    assert event.kind == "position" and event.data["id"] == 1
    assert event.data["position"] == [-4097, 8193]
    assert parse_line(line.replace("broadcasting", "handling")) is None


def test_unit_parser_recovers_fixed_point_hitbox_radius():
    event = parse_line(
        broadcast(
            "EventOnUnitAdded-UnitHandle:(EntityHandle:99)-UnitData:UnitData-Enemy (fixture)-"
            "Team:Enemy-WorldPosition:(2, 2)-HitboxRadius:1.199951-IsResumingRun:False"
        )
    )
    assert event.data["hitbox_radius"] == 4915
