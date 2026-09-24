import json
import os
from hashlib import sha256

import pytest

from inkbound_meter.model import Meter
from inkbound_meter.parser import Event, parse_line, parse_safe
from inkbound_meter.service import Session, replay
from inkbound_meter.store import Store

from .helpers import broadcast, damage


def record(entity, guid):
    return broadcast(
        "EventOnPlayerRecordUpdated-WorldStateChangePlayerRecordUpdated "
        f"(EntityHandle:{entity}) [{guid}]"
    )


def shield(amount=10, target=2, balance=10, combat=True):
    return broadcast(
        "EventOnUnitGainedEnergyShield-WorldStateChangeAddEnergyShieldToUnit-"
        f"UnitEntityHandle:(EntityHandle:{target})-Value:{amount}-HpEnergyShield:"
        f"hp:50-energyShield:{balance}-maxHp:50-UnitHandle:(EntityHandle:{target})-"
        f"UnitTeam:Friendly-IsInActiveCombat:{combat}"
    )


def heal(amount=1, hp=50, target=2, combat=True, flow="RealNotPredicted", action=None):
    action = action or "VestigeAll_Common_VerdantSeed_Heal_Action"
    return broadcast(
        "EventOnUnitHealed-WorldStateChangeHealUnit-"
        f"UnitEntityHandle:(EntityHandle:{target})-Value:{amount}-HpEnergyShield:"
        f"hp:{hp}-energyShield:0-maxHp:50-TargetUnitHandle:(EntityHandle:{target})-"
        f"TargetUnitTeam:Friendly-IsInActiveCombat:{combat}-HealAmount:{amount}-"
        f"ActionData:ActionData-{action} (fixture)-AbilityData:(none)-"
        "StatusEffectData:StatusEffectData-VestigeAll_Common_VerdantSeed_StatusEffect (fixture)-"
        f"LootableData:(none)-PredId-fixture--{flow}"
    )


def health(hp=50, balance=0, target=2):
    return broadcast(
        "EventOnUnitHpEnergyShieldStatChanged-WorldStateChangeModifyHpEnergyShieldStat-"
        f"UnitEntityHandle:(EntityHandle:{target})-Value:0-HpEnergyShield:"
        f"hp:{hp}-energyShield:{balance}-maxHp:50-UnitHandle:(EntityHandle:{target})-"
        "UnitTeam:Friendly-IsInActiveCombat:True"
    )


def incoming(amount, hp, **options):
    return (
        f"0T12:00:00 01 I Client unit state damaging unit (EntityHandle:2). "
        f"Attacker-(EntityHandle:9) : Damage Amount-{amount} : Ability fixture New hp: {hp}\n"
        + damage(amount, source=9, target=2, TargetUnitTeam="Friendly", **options)
    )


def setup_log():
    return (
        "0T12:00:00 01 I Client build number: 24243\n"
        + broadcast("EventPartyRunCreate")
        + broadcast("EventOnRunConnected-IsOnConnected:True-RunSeed:1234")
        + "".join(
            broadcast(
                f"EventOnUnitAdded-UnitHandle:(EntityHandle:{entity})-"
                "UnitData:UnitData-BasePlayerData (fixture)-CharacterClassType:C03-Team:Friendly-"
                "hp:50-energyShield:0-maxHp:50-IsResumingRun:False"
            )
            + f"0T12:00:00 01 I {name} (EntityHandle:{entity}) is playing ability Fixture\n"
            for entity, name in ((1, "Healer"), (2, "Tank"))
        )
        + broadcast(
            "EventOnUnitAdded-UnitHandle:(EntityHandle:9)-"
            "UnitData:UnitData-Enemy (fixture)-Team:Enemy"
        )
        + broadcast("EventOnCombatStarted-CombatZoneHandle:(EntityHandle:99)")
    )


def support_log():
    return (
        setup_log()
        + record(1, "EDclDmMI")
        + record(2, "KuFLDDDD")
        + shield()
        + record(2, "EDclDmMI")
        + record(2, "KuFLDDDD")
        + shield(5, balance=15)
        + incoming(12, 50)
        + health()
        + incoming(10, 40)
        + health(hp=49)
        + record(1, "hkIlI6PO")
        + record(1, "SYuK7HPf")
        + record(2, "sI7wIL3j")
        + heal(flow="Prediction")
        + heal(flow="RealAfterPrediction")
        + record(1, "hkIlI6PO")
        + record(2, "sI7wIL3j")
        + heal(amount=0)
        + broadcast("EventOnCombatEndSequenceEnded")
    )


def apply_log(text):
    meter = Meter()
    for line in text.splitlines():
        if event := parse_safe(line):
            meter.apply(event)
    return meter


def player(meter, metric, entity=1, scope="run"):
    return next(
        p for p in meter.snapshot(scope=scope)["metrics"][metric]["players"] if p["id"] == entity
    )


def components(row):
    return {v["name"]: v["damage"] for v in row["components"]}


def test_provider_totals_include_overheal_and_unused_shield_with_fifo_spending():
    meter = apply_log(support_log())
    assert meter.parse_errors == 0
    assert meter.snapshot()["party_damage"] == 0
    assert player(meter, "taken", 2)["amount"] == 22
    assert components(player(meter, "taken", 2)) == {"Absorbed": 12, "Health lost": 10}
    assert components(player(meter, "shielding", 1)) == {"Absorbed": 10}
    assert components(player(meter, "shielding", 2)) == {"Absorbed": 2, "Unused shield": 3}
    assert player(meter, "healing", 1)["amount"] == 4
    assert components(player(meter, "healing", 1)) == {"HP restored": 1, "Overheal": 3}
    assert player(meter, "healing", 2)["amount"] == 0
    assert player(meter, "healing", 1)["unknown_overheal"] == 0
    assert player(meter, "pressure", 2)["amount"] == 2
    for metric in meter.snapshot()["metrics"].values():
        for row in metric["players"]:
            assert sum(c["damage"] for c in row["components"]) == row["amount"]


def test_shield_bypass_and_overkill_ambiguity_do_not_invent_absorption():
    start = setup_log() + record(1, "EDclDmMI") + record(2, "KuFLDDDD") + shield()
    bypass = apply_log(start + incoming(5, 45))
    assert components(player(bypass, "taken", 2)) == {"Health lost": 5}
    assert components(player(bypass, "shielding")) == {"Active shield": 10}
    ambiguous = apply_log(start + incoming(100, 0))
    assert components(player(ambiguous, "taken", 2)) == {"Health lost": 50, "Unresolved": 50}
    assert components(player(ambiguous, "shielding")) == {"Unresolved": 10}


def test_unknown_providers_stale_records_and_unknown_formulas_stay_explicit():
    text = setup_log() + record(1, "EDclDmMI") + record(1, "KuFLDDDD") + shield()
    meter = apply_log(text)
    assert meter.snapshot()["metrics"]["shielding"]["unattributed"] == 10
    assert player(meter, "shielding")["amount"] == 0
    for line in (
        record(1, "hkIlI6PO") + record(2, "sI7wIL3j") + heal(action="NewHealAction")
    ).splitlines():
        meter.apply(parse_line(line))
    assert player(meter, "healing")["amount"] == 1
    assert player(meter, "healing")["unknown_overheal"] == 1
    stale = record(1, "EDclDmMI") + record(2, "KuFLDDDD") + shield().replace("12:00:00", "12:00:01")
    assert apply_log(setup_log() + stale).snapshot()["metrics"]["shielding"]["unattributed"] == 10


def test_status_ownership_credits_the_caster_and_requires_unique_owner():
    meter = apply_log(setup_log())
    status = {
        "target": 2,
        "source": 1,
        "instance": 1,
        "effect": "VestigeAll_Common_VerdantSeed_StatusEffect",
        "stacks": 1,
    }
    meter.apply(Event("status", data=status))
    meter.apply(parse_line(heal()))
    assert player(meter, "healing")["amount"] == 2
    meter.apply(Event("status", data={**status, "instance": 2, "source": 2}))
    meter.apply(parse_line(heal()))
    assert player(meter, "healing")["amount"] == 2
    assert meter.snapshot()["metrics"]["healing"]["unattributed"] == 1


def test_healing_modifiers_use_provider_and_recipient_stats_and_unknown_build_is_explicit():
    meter = apply_log(setup_log())
    meter.apply(Event("stat", data={"id": 1, "stat": "pROS9yE0", "value": 100}))
    meter.apply(Event("stat", data={"id": 2, "stat": "1SDlVYuI", "value": 25}))
    transaction = record(1, "hkIlI6PO") + record(2, "sI7wIL3j") + heal()
    for line in transaction.splitlines():
        meter.apply(parse_line(line))
    assert components(player(meter, "healing")) == {"HP restored": 1, "Overheal": 2}
    meter.apply(Event("game_build", data={"build": 99999}))
    for line in transaction.splitlines():
        meter.apply(parse_line(line))
    assert components(player(meter, "healing")) == {"HP restored": 2, "Overheal": 2}
    assert player(meter, "healing")["unknown_overheal"] == 1


def test_pressure_counts_dodges_but_excludes_dots_and_environment():
    meter = apply_log(
        setup_log()
        + incoming(0, 50, WasDodged="True")
        + incoming(3, 47, StatusEffectData="StatusEffectData-Poison (fixture)")
    )
    assert components(player(meter, "pressure", 2)) == {"Dodged": 1}
    assert player(meter, "taken", 2)["amount"] == 3
    meter.apply(Event("unit", data={"id": 9, "team": "Neutral"}))
    for line in incoming(3, 44).splitlines():
        meter.apply(parse_line(line))
    assert player(meter, "pressure", 2)["amount"] == 1


def test_out_of_combat_support_is_run_only_and_history_boundaries_clear_attribution():
    meter = apply_log(support_log())
    for line in (record(1, "hkIlI6PO") + record(2, "sI7wIL3j") + heal(combat=False)).splitlines():
        meter.apply(parse_line(line))
    assert player(meter, "healing")["amount"] == 6
    assert player(meter, "healing", scope="encounter")["amount"] == 4
    meter.apply(Event("combat_start", data={"zone": 100}))
    assert player(meter, "healing", scope="encounter")["amount"] == 0
    meter.apply(Event("support_record", data={"id": 1, "record": "EDclDmMI"}))
    meter.apply(Event("source_started"))
    for line in (setup_log() + record(2, "KuFLDDDD") + shield()).splitlines():
        meter.apply(parse_line(line))
    assert meter.snapshot()["metrics"]["shielding"]["unattributed"] == 10
    assert player(meter, "shielding")["amount"] == 0


@pytest.mark.parametrize("flow", ["Prediction", "Misprediction"])
def test_heal_predictions_are_not_counted(flow):
    assert parse_line(heal(flow=flow)) is None


def test_support_diagnostics_do_not_double_count_and_invalid_values_are_visible():
    assert (
        parse_line(shield().replace("[EventSystem] broadcasting", "System handling event:")) is None
    )
    assert parse_safe(heal().replace("HealAmount:1", "HealAmount:-1")).kind == "parse_error"
    assert parse_safe(shield().replace("Value:10", "Value:NaN")).kind == "parse_error"
    assert parse_safe(heal(flow="NewPredictionState")).kind == "parse_error"


def test_v2_upgrade_backfills_support_without_changing_damage_and_restart_matches_replay(tmp_path):
    log = tmp_path / "game.txt"
    log.write_text(support_log() + damage(706))
    raw = log.read_bytes()
    database = tmp_path / "meter.sqlite3"
    stat = log.stat()
    key = os.path.normcase(str(log.resolve()))
    old = []
    position = 0
    with Store(database) as store:
        cursor, _ = store.begin(key, f"{stat.st_dev}:{stat.st_ino}", raw[:256])
        for line in raw.splitlines(keepends=True):
            event = parse_safe(line.decode())
            if event and event.kind not in (
                "support_record",
                "health",
                "shield",
                "heal",
                "damage_health",
            ):
                data = {
                    k: v
                    for k, v in event.data.items()
                    if k not in ("hp", "shield", "max_hp", "support_version")
                }
                old.append((position, Event(event.kind, event.timestamp, data)))
            position += len(line)
        store.append(cursor, old, len(raw), sha256(raw[-512:]).hexdigest())
        store.db.execute("UPDATE streams SET parser_version=2")
        store.db.commit()
    with_session = Session(log, database)
    try:
        assert not with_session.meter.snapshot()["support_available"]
        assert with_session.meter.snapshot()["party_damage"] == 706
        assert with_session.poll().rebuild
        assert with_session.meter.report() == replay(log).report()
        for offset, event in old:
            stored = with_session.store.db.execute(
                "SELECT kind,timestamp,data FROM events WHERE byte_offset=?", (offset,)
            ).fetchone()
            assert (stored[0], stored[1]) == (event.kind, event.timestamp)
            assert json.loads(stored[2]).items() >= event.data.items()
    finally:
        with_session.close()
    restarted = Session(log, database)
    try:
        assert not restarted.poll().rebuild
        assert restarted.meter.report() == replay(log).report()
    finally:
        restarted.close()
