# Live party state

The party window displays current state independently of encounter/run totals.
It includes the local player and members of the same party: HP/current maximum,
shield, will/current turn allotment, active binding cooldowns in slot order,
guaranteed critical hits, turn phase, and players who have ended their turn or are downed.
Will can exceed the normal allotment after bonuses. A binding check means **off
cooldown**, not a promise that targeting, will or other casting conditions are satisfied.

## Sources

The inspected client's `EventOnUnitResourceModified` logs absolute will values as
`ManaPoints`. HP and shield snapshots are logged too. However,
`WorldStateChangeModifyAbilityCooldowns.ToString()` logs only a unit handle; it omits
the binding, amount and remaining turns. Cast/turn inference would miss resets and
selective cooldown reductions.

On 64-bit Windows, a separate worker samples the running `Inkbound.exe` every 250 ms.
It opens the process with `VM_READ`, `QUERY_LIMITED_INFORMATION` and `SYNCHRONIZE` only.
No process writes, remote threads, allocations, injected DLLs, suspension or calls
into the game are used. The sampler walks known Mono metadata and the live object
graph; it does not search or dump the process heap.

The root is `AppManager._instance → _clientApp → _applicationState → WorldClient →
predictedWorldState`, the state used by the game's own UI. Party members are selected
from player property records and matched to the local unit's party. Account IDs and
network/session records are neither collected nor emitted. Only the party identifier
comparison is used internally to filter the roster.

Values come from `UnitState` stats, `UnitResourceState.manaPoints`, and each
`AbilityInUseState.cooldownTurnCount`, `locationIndex`, `abilityDataId` and `willCrit`.
HP maximum follows `UnitCombatDBHelper.GetUnitMaxHealth` including modifier rounding;
duplicate stat entries are summed. Active binding slots exclude passive abilities.
Labels include ascended bindings, from 211 local English ability names. No game art
or decompiled source is distributed.

## Compatibility and availability

Client build **24243** and both installed binaries must match the inspected hashes
before any managed layout traversal:

| Binary | SHA-256 |
| --- | --- |
| `Assembly-CSharp.dll` | `abf0c92bdd56d6d88fa6f3ad15833334ad548e42a3f605954dba47bc67f41de3` |
| `mono-2.0-bdwgc.dll` | `2acef5eaf1425f84c83fc41e1f599e5ee7ac521e66fa2818ceb984fb8c02b8a4` |

Managed field offsets are resolved by name; Mono native layout offsets are gated
by the runtime hash. The implementation was checked against the installed runtime
and [Unity's Mono structures](https://github.com/Unity-Technologies/mono/blob/unity-2022.3-mbe/mono/metadata/class-private-definition.h).
Reads use bounded sizes, list versions and world/frame consistency checks. Failed
or inconsistent reads discard the snapshot and retry. Handles and caches are
released when the game exits, then reacquired on a subsequent launch.

The UI removes memory cooldown values immediately on an unavailable result and
after two seconds without a worker update. A missing game or empty live roster
clears the party display. Otherwise unavailable memory falls back to **last logged**
health/will with unknown cooldowns; the amber indicator explains the source.
Replay uses recorded values only, never the currently running game's memory.

The combat capture continues independently if the party reader is unavailable.
Snapshots stay in memory and are not inserted into SQLite or exported reports.
The meter can display live names before a player's first cast when the sampler's
`WorldGenerationDB.rngSeed` and complete entity roster match the active logged run.
This annotation is removed on stale/unavailable memory, disconnection, or run end,
and is never applied to replays or historical reports.
Parser version 4 backfills the fallback resource/turn records from intact logs
without altering existing damage or support events.

## Validation

`tests/test_party.py` covers a four-player object graph, foreign-party exclusion,
slot ordering, direct cooldown changes, health rounding, corrupt/changing lists,
process restart, absence, parser migration, UI controls and stale-value removal.

Run `scripts/check_windows_party.py --output <report.json> --screenshots <directory>`
with native Windows Python for independent dragging, native click-through, shared
visibility/opacity, menu recovery, cooldown tooltips, narrow four-player layouts,
and a read-only sample from a running game. It does not send inputs to Inkbound.
Use `scripts/check_windows_overlay.py` for the existing meter's native regressions.
