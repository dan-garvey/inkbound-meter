from pathlib import Path

import pytest

from inkbound_meter.parser import Event
from inkbound_meter.service import Session, replay
from inkbound_meter.store import Store

from .helpers import damage, start


def drain(session):
    while session.poll().catching_up:
        pass


def test_replay_and_incremental_reader_have_identical_results(tmp_path):
    log = Path(__file__).parent / "fixtures/first_encounter.txt"
    session = Session(log, tmp_path / "meter.sqlite3")
    session.reader.CHUNK = 4096  # Exercise records split at arbitrary chunk boundaries.
    drain(session)
    assert session.meter.report() == replay(log).report()
    session.close()


def test_restart_and_append_are_exactly_once_even_with_identical_lines(tmp_path):
    log, db = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    log.write_text(start() + damage(25))
    session = Session(log, db)
    drain(session)
    assert session.meter.snapshot()["party_damage"] == 25
    session.close()
    session = Session(log, db)
    drain(session)
    assert session.meter.snapshot()["party_damage"] == 25
    with log.open("a") as stream:
        stream.write(damage(25))
    drain(session)
    assert session.meter.snapshot()["party_damage"] == 50
    assert len(session.meter.runs) == 1
    session.close()


def test_partial_line_including_utf8_is_not_checkpointed(tmp_path):
    log, db = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    header = start().encode()
    line = "0T12:00:00 01 I Zoë (EntityHandle:1) is playing ability Bonk\n".encode()
    cut = line.index("ë".encode()) + 1
    log.write_bytes(header + line[:cut])
    session = Session(log, db)
    drain(session)
    assert session.store.cursor(session.reader.key)["offset"] == len(header)
    session.close()
    with log.open("ab") as stream:
        stream.write(line[cut:] + damage().encode())
    session = Session(log, db)
    drain(session)
    assert session.meter.snapshot()["players"][0]["name"] == "Zoë"
    assert session.meter.snapshot()["party_damage"] == 25
    session.close()


@pytest.mark.parametrize("replace_file", [False, True])
def test_log_truncation_and_replacement_create_new_generation(tmp_path, replace_file):
    log, db = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    log.write_text(start() + damage(100))
    session = Session(log, db)
    drain(session)
    if replace_file:
        log.rename(tmp_path / "previous.txt")
    else:
        log.write_text("")
        assert session.poll().status == "waiting for game"
    log.write_text(start() + damage(200))
    drain(session)
    assert [r["party_damage"] for r in session.meter.report()["runs"]] == [100, 200]
    session.close()


def test_truncate_then_regrow_with_same_prefix_detected_by_checkpoint_tail(tmp_path):
    log = tmp_path / "game.txt"
    header = ("Unrelated header\n" * 50) + start()
    log.write_text(header + damage(100))
    session = Session(log, tmp_path / "meter.sqlite3")
    drain(session)
    log.write_text(header + damage(222) + damage(333))
    drain(session)
    assert [r["party_damage"] for r in session.meter.report()["runs"]] == [100, 555]
    session.close()


def test_initial_small_file_can_grow_without_spurious_rotation(tmp_path):
    log = tmp_path / "game.txt"
    log.write_text("header\n")
    session = Session(log, tmp_path / "meter.sqlite3")
    drain(session)
    with log.open("a") as stream:
        stream.write(start() + damage())
    drain(session)
    assert sum(e.kind == "source_started" for e in session.store.events()) == 1
    session.close()


def test_missing_log_can_appear_after_startup(tmp_path):
    log = tmp_path / "game.txt"
    session = Session(log, tmp_path / "meter.sqlite3")
    assert session.poll().status == "waiting for log"
    log.write_text(start() + damage())
    drain(session)
    assert session.meter.snapshot()["party_damage"] == 25
    session.close()


def test_journal_and_checkpoint_commit_atomically(tmp_path):
    with Store(tmp_path / "meter.sqlite3") as store:
        cursor, _ = store.begin("fixture", "file-1", b"header")

        def broken_batch():
            yield (0, Event("run_create"))
            raise RuntimeError("Simulated write failure")

        with pytest.raises(RuntimeError):
            store.append(cursor, broken_batch(), 123, "tail")
        assert store.cursor("fixture")["offset"] == 0
        assert [e.kind for e in store.events()] == ["source_started"]


def test_future_database_schema_is_not_overwritten(tmp_path):
    db = tmp_path / "meter.sqlite3"
    with Store(db) as store:
        store.db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported"):
        Store(db)


def test_two_live_readers_cannot_race_on_same_checkpoint(tmp_path):
    log, db = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    first = Session(log, db)
    with pytest.raises(OSError, match="already capturing"):
        Session(log, db)
    first.close()
    second = Session(log, db)
    second.close()


def test_log_handle_does_not_prevent_game_rotation(tmp_path):
    from inkbound_meter.fileio import open_log

    log = tmp_path / "game.txt"
    log.write_bytes(b"original\n")
    with open_log(log) as stream:
        log.rename(tmp_path / "previous.txt")
        log.write_bytes(b"replacement\n")
        assert stream.read() == b"original\n"
    assert log.read_bytes() == b"replacement\n"


def test_v1_history_enrichment_is_ordered_atomic_and_preserves_every_damage(tmp_path):
    import json
    import os
    from hashlib import sha256

    from inkbound_meter.parser import parse_safe

    log = tmp_path / "game.txt"
    log.write_bytes((Path(__file__).parent / "fixtures/frostbite_components.txt").read_bytes())
    db = tmp_path / "meter.sqlite3"
    stat = log.stat()
    key = os.path.normcase(str(log.resolve()))
    raw = log.read_bytes()
    with Store(db) as store:
        cursor, _ = store.begin(key, f"{stat.st_dev}:{stat.st_ino}", raw[:256])
        position = 0
        old_records = []
        for line in raw.splitlines(keepends=True):
            event = parse_safe(line.decode())
            if event and event.kind not in ("stat", "status", "game_build"):
                data = {k: v for k, v in event.data.items() if k not in ("unit_data", "resuming")}
                old_records.append((position, Event(event.kind, event.timestamp, data)))
            position += len(line)
        store.append(cursor, old_records, len(raw), sha256(raw[-512:]).hexdigest())
        damage_before = [
            tuple(r)
            for r in store.db.execute(
                "SELECT byte_offset,data FROM events WHERE kind='damage' ORDER BY byte_offset"
            )
        ]
        store.db.execute("ALTER TABLE streams DROP COLUMN parser_version")
        store.db.execute("PRAGMA user_version=1")
        store.db.commit()
    session = Session(log, db)
    before = session.meter.snapshot(scope="run")["party_damage"]
    result = session.poll()
    assert result.rebuild
    assert before == session.meter.snapshot(scope="run")["party_damage"] == 21798
    assert session.meter.report() == replay(log).report()
    after = [
        tuple(r)
        for r in session.store.db.execute(
            "SELECT byte_offset,data FROM events WHERE kind='damage' ORDER BY byte_offset"
        )
    ]
    assert [(p, json.loads(d)) for p, d in after] == [(p, json.loads(d)) for p, d in damage_before]
    session.close()
    session = Session(log, db)
    assert not session.poll().rebuild
    assert session.meter.report() == replay(log).report()
    session.close()


def test_incompatible_history_is_never_replaced_by_enrichment(tmp_path):
    with Store(tmp_path / "meter.sqlite3") as store:
        cursor, _ = store.begin("fixture", "file-1", b"header")
        old = Event("damage", data={"amount": 706})
        store.append(cursor, [(100, old)], 200, "tail")
        assert not store.enrich(
            cursor, [(50, Event("stat")), (100, Event("damage", data={"amount": 700}))]
        )
        assert list(store.events())[-1] == old
        assert store.cursor("fixture")["offset"] == 200
        assert not any(e.kind == "stat" for e in store.events())


def test_v4_history_recovers_cast_positions_and_components_without_changing_hits(tmp_path):
    import json
    import os
    from hashlib import sha256

    from inkbound_meter.parser import PARSER_VERSION, parse_safe

    from .helpers import broadcast

    ability = "RunicStrikeUpgrade_Legendary_RecklessLunge_AbilityData"
    log, database = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    log.write_text(
        "0T12:00:00 00 I Client build number: 24243\n"
        + start()
        + broadcast("EventOnCombatStarted-CombatZoneHandle:(EntityHandle:99)")
        + broadcast(
            "EventOnUnitAdded-UnitHandle:(EntityHandle:1)-"
            "UnitData:UnitData-BasePlayerData (fixture)-CharacterClassType:C08-"
            "WorldPosition:(-1, -2)-IsResumingRun:False"
        )
        + broadcast(
            "EventOnUnitAdded-UnitHandle:(EntityHandle:99)-"
            "UnitData:UnitData-FigmentShield_Huge_EDG_T1_Unit (fixture)-Team:Enemy-"
            "WorldPosition:(2, 2)-IsResumingRun:False"
        )
        + broadcast(
            f"EventOnUnitPlayedAbility-UnitEntityHandle:(EntityHandle:1)-"
            f"AbilityData:AbilityData-{ability} (fixture)-FromWorldPosition:(-1, -2)"
        )
        + damage(
            240,
            action="RunicStrikeUpgrade_Legendary_RecklessLunge_PD_Distance_Action",
            AbilityData=f"AbilityData-{ability} (fixture)",
        )
    )
    raw = log.read_bytes()
    file_stat = log.stat()
    with Store(database) as store:
        cursor, _ = store.begin(
            os.path.normcase(str(log.resolve())),
            f"{file_stat.st_dev}:{file_stat.st_ino}",
            raw[:256],
        )
        records, position = [], 0
        for line in raw.splitlines(keepends=True):
            event = parse_safe(line.decode())
            if event and event.kind != "position":
                data = {k: v for k, v in event.data.items() if k != "position"}
                records.append((position, Event(event.kind, event.timestamp, data)))
            position += len(line)
        store.append(cursor, records, len(raw), sha256(raw[-512:]).hexdigest())
        store.db.execute("UPDATE streams SET parser_version=4")
        store.db.commit()
        old_hits = [
            tuple(r)
            for r in store.db.execute(
                "SELECT sequence,byte_offset,data FROM events WHERE kind='damage'"
            )
        ]
    session = Session(log, database)
    before = session.meter.snapshot()["players"][0]["sources"][0]["breakdown"]
    assert before["unresolved_damage"] == 240
    assert session.poll().rebuild
    assert session.meter.report() == replay(log).report()
    after = session.meter.snapshot()["players"][0]["sources"][0]["breakdown"]
    assert after["matched_damage"] == 240 and after["unresolved_damage"] == 0
    assert {v["name"]: v["damage"] for v in after["components"]}["Distance bonus"] == 125
    new_hits = [
        tuple(r)
        for r in session.store.db.execute(
            "SELECT sequence,byte_offset,data FROM events WHERE kind='damage'"
        )
    ]
    assert [(s, p, json.loads(d)) for s, p, d in new_hits] == [
        (s, p, json.loads(d)) for s, p, d in old_hits
    ]
    assert session.store.cursor(session.reader.key)["parser_version"] == PARSER_VERSION
    session.close()
    restarted = Session(log, database)
    assert not restarted.poll().rebuild
    assert restarted.meter.report() == replay(log).report()
    restarted.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_burn_repair_survives_restart_split_records_and_v5_history(tmp_path, legacy):
    import json
    import os
    from hashlib import sha256

    from inkbound_meter.parser import PARSER_VERSION, parse_safe

    from .helpers import broadcast

    log, database = tmp_path / "game.txt", tmp_path / "meter.sqlite3"
    head = (
        "0T12:00:00 00 I Client build number: 24243\n"
        + start()
        + broadcast("EventOnCombatStarted-CombatZoneHandle:(EntityHandle:99)")
        + broadcast(
            "EventOnUnitAdded-UnitHandle:(EntityHandle:1)-"
            "UnitData:UnitData-BasePlayerData (fixture)-CharacterClassType:C01-"
            "IsResumingRun:False"
        )
        + broadcast(
            "EventOnUnitAdded-UnitHandle:(EntityHandle:99)-"
            "UnitData:UnitData-FigmentShield_Huge_EDG_T1_Unit (fixture)-Team:Enemy-"
            "IsResumingRun:False"
        )
        + broadcast(
            "EventOnUnitStatusEffectStacksAdded-WorldStateChangeUnitAddStatusEffectStacks-"
            "TargetUnitEntityHandle:(EntityHandle:99)-CasterUnitEntityHandle:(EntityHandle:1)-"
            "StatusEffectInstanceHandle:(Handle:15)-"
            "StatusEffectData:StatusEffectData-Burn_StatusEffect (fixture)-"
            "StacksAdded:2-NewStacksValue:2"
        )
        + broadcast(
            "EventOnUnitStatusEffectAction-WorldStateChangeUnitStatusEffectAction-"
            "UnitEntityHandle:(EntityHandle:99)-ContextTargetUnitEntityHandle:(EntityHandle:99)-"
            "StatusEffect:Burn_StatusEffect-sXmQNYjg-ProcType:OnTurnEnd-SkipPause:False"
        )
        + damage(
            30,
            action="Burn_Damage_Action",
            AbilityData="(none)",
            StatusEffectData="StatusEffectData-Burn_StatusEffect (fixture)",
        )
    )
    tail = broadcast(
        "EventOnUnitStatusEffectStacksRemoved-WorldStateChangeUnitRemoveStatusEffectStacks-"
        "TargetUnitEntityHandle:(EntityHandle:99)-CasterUnitEntityHandle:(EntityHandle:1)-"
        "StatusEffectInstanceHandle:(Handle:15)-"
        "StatusEffectData:StatusEffectData-Burn_StatusEffect (fixture)-"
        "StacksRemoved:1-NewStacksValue:2"
    )
    log.write_text(head + tail if legacy else head + tail[:100])
    if legacy:
        raw = log.read_bytes()
        file_stat = log.stat()
        with Store(database) as store:
            cursor, _ = store.begin(
                os.path.normcase(str(log.resolve())),
                f"{file_stat.st_dev}:{file_stat.st_ino}",
                raw[:256],
            )
            position, records = 0, []
            for line in raw.splitlines(keepends=True):
                event = parse_safe(line.decode())
                if event and event.kind != "status_proc":
                    data = {k: v for k, v in event.data.items() if k not in ("added", "removed")}
                    records.append((position, Event(event.kind, event.timestamp, data)))
                position += len(line)
            store.append(cursor, records, len(raw), sha256(raw[-512:]).hexdigest())
            store.db.execute("UPDATE streams SET parser_version=5")
            store.db.commit()
    session = Session(log, database)
    if not legacy:
        drain(session)
    before = session.meter.snapshot()["players"][0]["sources"][0]["breakdown"]
    assert before["unresolved_damage"] == 30
    old_hits = [
        tuple(r)
        for r in session.store.db.execute(
            "SELECT sequence,byte_offset,data FROM events WHERE kind='damage'"
        )
    ]
    session.close()
    if not legacy:
        with log.open("a") as stream:
            stream.write(tail[100:])
    session = Session(log, database)
    session.reader.CHUNK = 1024
    drain(session)
    assert session.meter.report() == replay(log).report()
    after = session.meter.snapshot()["players"][0]["sources"][0]["breakdown"]
    assert (after["hits"], after["matched_hits"], after["matched_damage"]) == (1, 1, 30)
    assert after["latest"]["stacks"] == 3
    assert after["unresolved_damage"] == 0
    new_hits = [
        tuple(r)
        for r in session.store.db.execute(
            "SELECT sequence,byte_offset,data FROM events WHERE kind='damage'"
        )
    ]
    assert [(s, p, json.loads(d)) for s, p, d in old_hits] == [
        (s, p, json.loads(d)) for s, p, d in new_hits
    ]
    assert session.store.cursor(session.reader.key)["parser_version"] == PARSER_VERSION
    session.close()
    session = Session(log, database)
    drain(session)
    assert session.meter.report() == replay(log).report()
    session.close()
