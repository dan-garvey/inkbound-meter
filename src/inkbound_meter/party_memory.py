"""Sample the client's party state using read-only Mono object traversal."""

from __future__ import annotations

import hashlib
import time
from collections import Counter

from .memory import MemoryUnavailable, MonoReader, ProcessMemory
from .party import party_catalog

RUNTIME_SHA256 = "2acef5eaf1425f84c83fc41e1f599e5ee7ac521e66fa2818ceb984fb8c02b8a4"
ASSEMBLY_SHA256 = "abf0c92bdd56d6d88fa6f3ad15833334ad548e42a3f605954dba47bc67f41de3"
PHASES = (
    "INITIAL",
    "StartEnemyTurn",
    "EnemyTurn",
    "EndEnemyTurn",
    "StartPlayerTurn",
    "PlayerTurn",
    "EndPlayerTurn",
    "BoardEffects",
    "FINAL",
)


def max_health(stats):
    base = max(1, stats.get("VOj3zfre", 0))
    multiplier = 100 + stats.get("ogsnL3Pq", 0)
    product = base * multiplier
    return product // 100 + int(multiplier > 100 and product % 100 != 0)


class PartyMemory:
    def __init__(self, process_factory=ProcessMemory, clock=time.monotonic):
        self.process_factory = process_factory
        self.clock = clock
        self.process = None
        self.mono = None
        self.root_class = None
        self.retry_at = 0.0
        self.status = "Waiting for Inkbound"
        self._hashes = {}

    def _verify(self, path, expected):
        stat = path.stat()
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key not in self._hashes:
            with path.open("rb") as stream:
                self._hashes[key] = hashlib.file_digest(stream, "sha256").hexdigest()
        if self._hashes[key] != expected:
            raise MemoryUnavailable("Game update needs a new memory reader; using logs")

    def _attach(self):
        process = self.process_factory()
        try:
            base, runtime = process.modules.get("mono-2.0-bdwgc.dll", (None, None))
            _, exe = process.modules.get("inkbound.exe", (None, None))
            if not runtime or not exe:
                raise MemoryUnavailable("Waiting for game runtime")
            self._verify(runtime, RUNTIME_SHA256)
            self._verify(exe.parent / "Inkbound_Data/Managed/Assembly-CSharp.dll", ASSEMBLY_SHA256)
            mono = MonoReader(process, base)
            root_class = mono.find_class("ShinyShoe", "AppManager")
            self.process, self.mono, self.root_class = process, mono, root_class
        except Exception:
            process.close()
            raise

    def sample(self):
        """Never return an old memory snapshot after failure or game exit."""
        try:
            if self.process and not self.process.alive():
                self.close()
                self.retry_at = 0
            if not self.process:
                if self.clock() < self.retry_at:
                    return self._unavailable()
                self._attach()
            for _ in range(3):
                try:
                    return self._read_party()
                except MemoryUnavailable:
                    pass
            raise MemoryUnavailable("Party state is changing; using latest log values")
        except (OSError, ValueError, KeyError, UnicodeError) as exc:
            self.status = (
                str(exc) if isinstance(exc, MemoryUnavailable) else "Party memory unavailable"
            )
            self.close()
            self.retry_at = self.clock() + 3
            return self._unavailable()

    def _unavailable(self):
        return {
            "source": "unavailable",
            "players": [],
            "status": self.status,
            "phase": "",
            "turn": None,
        }

    def _read_party(self):
        from .model import CLASS_NAMES

        m, r = self.mono, self.process
        app = m.static_ref(self.root_class, "_instance")
        client = m.require_class(m.ref(app, "_clientApp"), "ClientApp")
        state = m.require_class(m.ref(client, "_applicationState"), "ClientApplicationState")
        if r.string(m.ref(state, "<BuildNumber>k__BackingField")) != str(party_catalog()["build"]):
            raise MemoryUnavailable("Unsupported game build")
        wc = m.require_class(m.ref(state, "<WorldClient>k__BackingField"), "WorldClient")
        world = m.require_class(m.ref(wc, "predictedWorldState"), "WorldState")
        local = m.integer(wc, "localEntHandle")
        frame = m.integer(wc, "currentSimFrame")
        units = self._db_list(world, "unitCombatDB", "unitStates")
        units = {m.integer(u, "entityHandle"): u for u in units}
        if local < 0 or local not in units:
            return {
                "source": "memory",
                "players": [],
                "status": "Waiting for party",
                "phase": "",
                "turn": None,
            }
        # Only the local party, including in the Atheneum where other players may exist.
        local_party = r.string(m.ref(units[local], "partyId"))
        roster = [
            m.integer(p, "ownerEntHandle")
            for p in self._db_list(world, "playerPropertyDB", "playerContexts")
        ]
        roster = [
            p
            for p in roster
            if p in units
            and (
                p == local or (local_party and r.string(m.ref(units[p], "partyId")) == local_party)
            )
        ]
        if local not in roster or len(roster) > 4 or len(set(roster)) != len(roster):
            raise MemoryUnavailable("Party roster is updating")
        resources = {
            m.integer(p, "entityHandle"): m.integer(p, "manaPoints")
            for p in self._db_list(world, "unitResourceDB", "unitResourceStates")
        }
        classes = {
            m.integer(p, "ownerEntHandle"): r.string(m.ref(p, "characterClassDataId"))
            for p in self._db_list(world, "characterClassDB", "characterClassContextList")
        }
        contexts = {
            m.integer(p, "ownerEntHandle"): p
            for p in self._db_list(world, "abilityDB", "abilityContexts")
        }
        phase, turn = "", None
        players = []
        for entity in sorted(roster, key=lambda p: (p != local, p)):
            unit = units[entity]
            stats = self._stats(unit)
            hp, shield = max(0, stats.get("nJj1wHLS", 0)), max(0, stats.get("A3xbQ1as", 0))
            maximum, will = max_health(stats), resources.get(entity)
            max_will = max(0, stats.get("bFwnYuVx", 0))
            if (
                will is None
                or any(not 0 <= v <= 1_000_000 for v in (hp, shield, maximum, will, max_will))
                or maximum < 1
            ):
                raise MemoryUnavailable("Invalid party resources")
            abilities = self._abilities(contexts.get(entity))
            class_id = party_catalog()["classes"].get(classes.get(entity), "")
            players.append(
                {
                    "id": entity,
                    "name": r.string(m.ref(unit, "displayName")),
                    "class": CLASS_NAMES.get(class_id, class_id),
                    "local": entity == local,
                    "hp": hp,
                    "max_hp": maximum,
                    "shield": shield,
                    "will": will,
                    "max_will": max_will,
                    "ended_turn": m.boolean(unit, "hasEndedTurn"),
                    "abilities": abilities,
                }
            )
        zone = m.integer(units[local], "combatZoneEntityHandle")
        for item in self._db_list(world, "combatZoneDB", "combatZones"):
            if m.integer(item, "entityHandle") == zone:
                machine = m.ref(m.ref(item, "state"), "turnPhaseStateMachine")
                phase_index = r.read(m.field(machine, "currentPhase"), 1)[0]
                if phase_index >= len(PHASES):
                    raise MemoryUnavailable("Invalid turn phase")
                phase = PHASES[phase_index]
                turn = m.integer(machine, "currentTurn") + 1
        seed = m.integer(m.ref(world, "worldGenerationDB"), "rngSeed")
        if (
            m.ref(wc, "predictedWorldState") != world
            or m.integer(wc, "currentSimFrame") != frame
            or m.integer(wc, "localEntHandle") != local
        ):
            raise MemoryUnavailable("World changed while reading")
        if phase in ("INITIAL", "FINAL"):
            phase, turn = "", None
        return {
            "source": "memory",
            "players": players,
            "phase": phase,
            "turn": turn,
            "seed": seed,
            "status": "Live party state",
        }

    def _db_list(self, world, database, field):
        return self.mono.objects(self.mono.ref(self.mono.ref(world, database), field))

    def _stats(self, unit):
        m, r = self.mono, self.process
        result = Counter()
        for entry in m.objects(m.ref(m.ref(unit, "stats"), "statEntries"), limit=1024):
            guid = r.string(m.ref(entry, "statDataGuid"), limit=32)
            if guid in ("nJj1wHLS", "A3xbQ1as", "VOj3zfre", "ogsnL3Pq", "bFwnYuVx"):
                result[guid] += m.integer(entry, "statValue")
        if "nJj1wHLS" not in result or "VOj3zfre" not in result:
            raise MemoryUnavailable("Party health is loading")
        return result

    def _abilities(self, context):
        if not context:
            return None
        m, r = self.mono, self.process
        result = []
        slots = set()
        for ability in m.objects(m.ref(context, "abilitiesInUse"), limit=32):
            slot = m.integer(ability, "locationIndex")
            if slot < 0:  # Passive abilities have no binding bar slot.
                continue
            guid = r.string(m.ref(ability, "abilityDataId"), limit=32)
            cooldown = m.integer(ability, "cooldownTurnCount")
            if not 0 <= cooldown <= 1000 or not 0 <= slot < 8 or slot in slots:
                raise MemoryUnavailable("Invalid binding state")
            slots.add(slot)
            definition = party_catalog()["abilities"].get(guid, {})
            result.append(
                {
                    "id": guid,
                    "slot": slot,
                    "name": definition.get("name", "Binding"),
                    "cooldown": cooldown,
                    "will_crit": m.boolean(ability, "willCrit"),
                }
            )
        return sorted(result, key=lambda a: a["slot"])

    def close(self):
        if self.process:
            self.process.close()
        self.process = self.mono = self.root_class = None
