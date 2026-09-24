import json
import os
import struct
from copy import deepcopy
from hashlib import sha256

import pytest

from inkbound_meter.memory import MemoryUnavailable, MonoReader, ProcessMemory
from inkbound_meter.model import Meter
from inkbound_meter.parser import Event, parse_safe
from inkbound_meter.party_memory import PartyMemory, max_health
from inkbound_meter.service import Session
from inkbound_meter.store import Store

from .helpers import damage, start


def broadcast(body):
    return f"0T12:00:00 01 I [EventSystem] broadcasting {body}\n"


RESOURCE = broadcast(
    "EventOnUnitResourceModified-WorldStateChangeModifyResource-"
    "UnitEntityHandle:(EntityHandle:1)-ResourceType:ManaPoints-"
    "PreviousValue:4-NewValue:2"
)


def test_party_log_resources_are_absolute_and_do_not_invent_cooldowns():
    meter = Meter()
    meter.apply(
        Event("player", data={"id": 1, "name": "Wisp", "hp": 30, "max_hp": 50, "shield": 4})
    )
    assert meter.party.snapshot(meter.support_context.health)["players"][0]["will"] is None
    for _ in range(2):
        meter.apply(parse_safe(RESOURCE))
    meter.apply(Event("stat", data={"id": 1, "stat": "bFwnYuVx", "value": 5}))
    view = meter.party.snapshot(meter.support_context.health)["players"][0]
    assert (view["will"], view["max_will"], view["hp"], view["max_hp"], view["shield"]) == (
        2,
        5,
        30,
        50,
        4,
    )
    assert view["abilities"] is None
    assert parse_safe(RESOURCE.replace("broadcasting", "handling")) is None
    assert (
        parse_safe(
            broadcast(
                "EventOnUnitAbilityCooldownsModified-"
                "WorldStateChangeModifyAbilityCooldowns-"
                "UnitEntityHandle:(EntityHandle:1)"
            )
        )
        is None
    )


def test_party_turns_departures_disconnects_and_reused_entity_ids():
    meter = Meter()
    meter.apply(Event("player", data={"id": 1, "name": "Before"}))
    meter.apply(parse_safe(RESOURCE))
    meter.apply(Event("combat_start", data={"zone": 4}))
    meter.apply(
        parse_safe(
            broadcast(
                "EventOnEndTurnRequestSucceeded-WorldStateChangeEndTurnAction-"
                "UnitEntityHandle:(EntityHandle:1)"
            )
        )
    )
    assert meter.party.players[1]["ended_turn"]
    for zone, expected in ((99, True), (4, False)):
        meter.apply(
            parse_safe(
                broadcast(
                    "EventOnTurnPhaseAdvanced-WorldStateChangeAdvanceTurnPhase-"
                    f"CombatZoneEntityHandle:(EntityHandle:{zone})-"
                    "NewTurnPhase:StartPlayerTurn"
                )
            )
        )
        assert meter.party.players[1]["ended_turn"] is expected
    meter.apply(
        parse_safe(
            broadcast(
                "EventOnUnitRemoved-WorldStateChangeUnitRemoved-EntityHandle:(EntityHandle:1)"
            )
        )
    )
    assert meter.party.snapshot({})["players"] == []
    meter.apply(Event("connection", data={"connected": False, "seed": 1}))
    assert meter.party.snapshot({})["players"] == []
    meter.apply(Event("connection", data={"connected": True, "seed": 2}))
    meter.apply(Event("player", data={"id": 1, "name": "After"}))
    view = meter.party.snapshot({})["players"][0]
    assert view["name"] == "After" and view["will"] is None and view["hp"] is None


def test_v3_journal_backfills_party_without_changing_existing_combat_totals(tmp_path):
    log = tmp_path / "game.txt"
    log.write_text(start() + RESOURCE + damage(42))
    raw, db = log.read_bytes(), tmp_path / "meter.sqlite3"
    stat, key = log.stat(), os.path.normcase(str(log.resolve()))
    old_meter = Meter()
    with Store(db) as store:
        cursor, beginning = store.begin(key, f"{stat.st_dev}:{stat.st_ino}", raw[:256])
        old_meter.apply(beginning)
        records, position = [], 0
        for line in raw.splitlines(keepends=True):
            event = parse_safe(line.decode())
            if event and event.kind != "resource":
                records.append((position, event))
                old_meter.apply(event)
            position += len(line)
        store.append(cursor, records, len(raw), sha256(raw[-512:]).hexdigest())
        store.db.execute("UPDATE streams SET parser_version=3")
        store.db.commit()
    session = Session(log, db)
    assert session.poll().rebuild
    # The new resource record supplies a Will-efficiency denominator, but cannot
    # change pre-existing combat totals or attribution.
    assert (
        session.meter.snapshot(scope="run")["party_damage"]
        == old_meter.snapshot(scope="run")["party_damage"]
    )
    assert session.meter.party.snapshot({})["players"][0]["will"] == 2
    from inkbound_meter.parser import PARSER_VERSION

    assert session.store.cursor(session.reader.key)["parser_version"] == PARSER_VERSION
    session.close()
    restarted = Session(log, db)
    assert not restarted.poll().rebuild
    assert restarted.meter.party.snapshot({})["players"][0]["will"] == 2
    restarted.close()


class FakeMemory:
    def __init__(self, data):
        self.data = data
        self.closed = False
        self.running = True

    def alive(self):
        return self.running

    def close(self):
        self.closed = True

    def read(self, address, size):
        return self.data[address][:size]

    @staticmethod
    def string(value, limit=160):
        return value


class ObjectReader:
    """A managed object graph, independent of the production pointer traversal."""

    @staticmethod
    def ref(obj, field):
        return obj[field]

    integer = boolean = field = ref

    @staticmethod
    def require_class(obj, name):
        assert obj["type"] == name
        return obj

    @staticmethod
    def static_ref(obj, name):
        return obj[name]

    @staticmethod
    def objects(obj, limit=1024):
        assert len(obj) <= limit
        return obj


def memory_fixture():
    # Four players and an unrelated party member in the same world.
    units, resources, contexts = [], [], []
    for entity in range(1, 6):
        units.append(
            {
                "entityHandle": entity,
                "partyId": "ours" if entity < 5 else "other",
                "displayName": f"Player {entity}",
                "hasEndedTurn": entity == 2,
                "combatZoneEntityHandle": 90,
                "stats": {
                    "statEntries": [
                        {"statDataGuid": guid, "statValue": value}
                        for guid, value in [
                            ("nJj1wHLS", 37),
                            ("VOj3zfre", 50),
                            ("VOj3zfre", 5),
                            ("ogsnL3Pq", 10),
                            ("A3xbQ1as", 8),
                            ("bFwnYuVx", 6),
                        ]
                    ]
                },
            }
        )
        resources.append({"entityHandle": entity, "manaPoints": entity})
        contexts.append(
            {
                "ownerEntHandle": entity,
                "abilitiesInUse": [
                    {
                        "locationIndex": slot,
                        "abilityDataId": guid,
                        "cooldownTurnCount": cooldown,
                        "willCrit": critical,
                    }
                    for slot, guid, cooldown, critical in [
                        (3, "aa8zcoaH", 3, False),
                        (0, "WX0p3rEX", 0, True),
                        (-1, "passive", 99, False),
                    ]
                ],
            }
        )
    world = {
        "type": "WorldState",
        "worldGenerationDB": {"rngSeed": 123456},
        "unitCombatDB": {"unitStates": units},
        "unitResourceDB": {"unitResourceStates": resources},
        "abilityDB": {"abilityContexts": contexts},
        "playerPropertyDB": {"playerContexts": [{"ownerEntHandle": i} for i in range(1, 6)]},
        "characterClassDB": {"characterClassContextList": []},
        "combatZoneDB": {
            "combatZones": [
                {
                    "entityHandle": 90,
                    "state": {"turnPhaseStateMachine": {"currentPhase": 100, "currentTurn": 2}},
                }
            ]
        },
    }
    wc = {
        "type": "WorldClient",
        "predictedWorldState": world,
        "localEntHandle": 3,
        "currentSimFrame": 42,
    }
    state = {
        "type": "ClientApplicationState",
        "<BuildNumber>k__BackingField": "24243",
        "<WorldClient>k__BackingField": wc,
    }
    app = {"_clientApp": {"type": "ClientApp", "_applicationState": state}}
    reader = PartyMemory()
    reader.mono = ObjectReader()
    reader.process = FakeMemory({100: b"\x05"})
    reader.root_class = {"_instance": app}
    return reader, world, wc


def test_memory_party_filters_members_preserves_slots_and_reads_actual_cooldowns():
    reader, world, _ = memory_fixture()
    data = reader.sample()
    assert data["source"] == "memory", data
    assert data["seed"] == 123456
    assert data["phase"] == "PlayerTurn" and data["turn"] == 3
    assert [p["id"] for p in data["players"]] == [3, 1, 2, 4]
    player = data["players"][0]
    assert (player["hp"], player["max_hp"], player["shield"], player["will"]) == (37, 61, 8, 3)
    assert [(a["name"], a["cooldown"]) for a in player["abilities"]] == [
        ("Poison Shot", 0),
        ("Cleave", 3),
    ]
    assert player["abilities"][0]["will_crit"]
    world["abilityDB"]["abilityContexts"][2]["abilitiesInUse"][0]["cooldownTurnCount"] = 0
    assert reader.sample()["players"][0]["abilities"][1]["cooldown"] == 0


def test_memory_failure_and_process_restart_never_return_old_snapshot():
    clock = [0.0]
    reader, world, _ = memory_fixture()
    reader.clock = lambda: clock[0]
    assert reader.sample()["players"]
    world["abilityDB"]["abilityContexts"][0]["abilitiesInUse"][0]["cooldownTurnCount"] = -1
    failed = reader.sample()
    assert failed["source"] == "unavailable" and failed["players"] == []
    assert reader.sample() == failed
    new, _, _ = memory_fixture()
    clock[0] = 4

    def attach():
        reader.process, reader.mono, reader.root_class = new.process, new.mono, new.root_class

    reader._attach = attach
    assert len(reader.sample()["players"]) == 4
    previous = reader.process
    previous.running = False
    new, _, _ = memory_fixture()
    assert len(reader.sample()["players"]) == 4
    assert previous.closed


def test_memory_world_without_local_player_clears_party():
    reader, _, wc = memory_fixture()
    assert reader.sample()["players"]
    wc["localEntHandle"] = -1
    assert reader.sample()["players"] == []


def test_memory_version_gate_and_access_are_read_only(tmp_path):
    path = tmp_path / "runtime.dll"
    path.write_bytes(b"new game build")
    with pytest.raises(MemoryUnavailable, match="Game update"):
        PartyMemory()._verify(path, "wrong")
    assert ProcessMemory.ACCESS & 0x10  # VM_READ
    assert not ProcessMemory.ACCESS & (0x20 | 0x08 | 0x02)  # VM_WRITE, VM_OPERATION, CREATE_THREAD
    assert max_health({"VOj3zfre": 51, "ogsnL3Pq": -10}) == 45


def test_mono_lists_reject_corruption_and_concurrent_mutation():
    class BytesMemory:
        def __init__(self):
            self.changing = False
            self.reads = 0

        def read(self, address, size):
            if address == 116:
                self.reads += 1
                count = 2 if not self.changing or self.reads == 1 else 3
                return struct.pack("<QiiQ", 200, count, 1, 0)
            assert address == 232 and size == 16
            return struct.pack("<QQ", 1000, 2000)

        def ptr(self, address):
            assert address == 224
            return 2

    memory = BytesMemory()
    reader = MonoReader(memory, 0)
    assert reader.objects(100) == [1000, 2000]
    with pytest.raises(MemoryUnavailable, match="length"):
        reader.objects(100, limit=1)
    memory.reads, memory.changing = 0, True
    with pytest.raises(MemoryUnavailable, match="changed"):
        reader.objects(100)


def party_sample():
    reader, _, _ = memory_fixture()
    data = reader.sample()
    for player in data["players"]:
        player["class"] = "Mosscloak"
        player["abilities"] += [
            {"id": str(i), "slot": i, "name": name, "cooldown": i, "will_crit": False}
            for i, name in enumerate(("Contaminate", "Flurry", "Discharge"), 4)
        ]
    return data


def test_party_window_independent_position_visibility_shared_hotkeys_and_opacity(qapp, tmp_path):
    from inkbound_meter.config import Settings
    from inkbound_meter.overlay import Overlay

    path = tmp_path / "settings.json"
    window = Overlay(path, register_hotkeys=False)
    window.party.update_memory(party_sample())
    window.show()
    qapp.processEvents()
    assert window.party.isVisible()
    original = window.pos()
    window.party.move(original.x() + 30, original.y() + 40)
    assert window.pos() == original
    window.set_opacity(0.6)
    assert abs(window.party.windowOpacity() - 0.6) < 0.005
    window.toggle_interaction()
    assert not window.interactive
    assert window.party.windowFlags() & window.party.windowFlags().WindowTransparentForInput
    window.toggle_visibility()
    assert not window.party.isVisible()
    window.toggle_visibility()
    assert window.party.isVisible()
    window.recover()
    window.party.close()
    assert window.isVisible() and not window.party.isVisible()
    assert not Settings.load(path).party_enabled
    window.toggle_visibility()
    window.toggle_visibility()
    assert not window.party.isVisible()
    window.set_party_enabled(True)
    assert window.party.isVisible()
    window.close()
    settings = Settings.load(path)
    assert settings.party_x == original.x() + 30 and settings.party_y == original.y() + 40


def test_party_widgets_stable_and_four_players_fit_small_width(qapp, tmp_path):
    from inkbound_meter.overlay import Overlay

    window = Overlay(tmp_path / "settings.json", register_hotkeys=False)
    data = party_sample()
    window.party.setFixedWidth(300)
    window.party.update_memory(data)
    window.show()
    qapp.processEvents()
    assert window.party.height() <= 550
    row = window.party.rows[3]
    tile = row.tiles[0]
    window.party.update_memory(deepcopy(data))
    assert window.party.rows[3] is row and row.tiles[0] is tile
    for row in window.party.rows.values():
        assert row.geometry().right() < window.party.width()
        assert row.geometry().bottom() < window.party.panel.height()
        for tile in row.tiles:
            assert tile.geometry().right() < row.width()
    new = deepcopy(data)
    new["players"][0]["abilities"][0]["cooldown"] = 2
    window.party.update_memory(new)
    assert window.party.rows[3].tiles[0].data["cooldown"] == 2
    assert "2 turns" in window.party.rows[3].tiles[0].toolTip()
    window.close()


def test_party_memory_outage_clears_cooldowns_and_shows_log_provenance(qapp, tmp_path):
    from inkbound_meter.overlay import Overlay, payload

    meter = Meter()
    meter.apply(Event("player", data={"id": 1, "name": "Logged"}))
    meter.apply(parse_safe(RESOURCE))
    window = Overlay(tmp_path / "settings.json", register_hotkeys=False)
    window.update_data(payload(meter, "live"))
    window.party.update_memory(party_sample())
    assert len(window.party.rows) == 4
    window.party.memory_at -= 3
    window.party.render()
    assert window.party.data["source"] == "log"
    assert window.party.rows[1].tiles == []
    assert "Last logged" in window.party.indicator.toolTip()
    window.party.update_memory(
        {"source": "unavailable", "players": [], "status": "Waiting for Inkbound"}
    )
    assert window.party.rows == {}
    window.close()


def test_party_catalog_has_real_ascended_names():
    from inkbound_meter.party import party_catalog

    catalog = party_catalog()
    assert catalog["abilities"]["2mBmUgtk"]["name"] == "Contaminate"
    assert json.loads(json.dumps(catalog)) == catalog
