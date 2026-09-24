# Data and counting contract

The pipeline is **complete log line → normalized event → SQLite journal → reducer → overlay**.
Saved-file replay uses the same parser and reducer. UI updates are delivered on the
Qt thread; the reader and its SQLite connection stay in a background thread.
An independent read-only memory sampler supplies the separate party window. Its
snapshots are ephemeral and do not change combat totals or write to the journal.

## Input events

| Evidence | Meaning |
| --- | --- |
| `EventPartyRunCreate` broadcast | The next connection was observed from creation. |
| `EventOnNewRunVoteFinished` broadcast | New-run vote before the next connection, also observed when the local client does not emit `EventPartyRunCreate`. |
| `EventOnRunConnected` broadcast | Start a new captured run, or leave it. Seeds are metadata, not unique IDs. |
| `EventOnUnitAdded` with `BasePlayerData` | Register a party member, even before their first attack. |
| `is playing ability` actor line | Associate a player handle with its complete display name. |
| `Setting unit class for animation` | Update the class of an already-known player. |
| Other `EventOnUnitAdded` | Identify enemy sources so self-damage is not assigned to players. |
| `EventOnCombatStarted` | Start an encounter; repeated announcements for the same zone are idempotent. |
| `EventOnCombatEndSequenceEnded` | Close the encounter and retain its totals for viewing. |
| `EventOnRunEnd` | Record victory/defeat; repeated announcements are idempotent. |
| `EventOnUnitDamaged` broadcast | Read source, target, amount, team, active-combat/dodge/critical flags, and action/ability/effect IDs. |
| `Client build number` | Select the inspected damage formula catalog; unknown builds have no component attribution. |
| `EventOnUnitStatModified` broadcast | Absolute new stat value, keyed by unit and stat GUID. Negative values are preserved. |
| Status-effect stacks added/removed broadcasts | Track caster, target, instance, effect and absolute new stacks. |
| Unit-added, moved, teleported and played-ability broadcasts | Record positions and cast origins on the 1/4096 coordinate grid for inspected distance formulas. Never add hits. |
| Shield-gained and authoritative healed broadcasts | Count granted shield and effective healing; prediction/rollback heals are excluded. |
| Relevant player-record broadcasts | Associate support provider and recipient; never add amounts by themselves. |
| HP/shield broadcasts and matching client damage HP diagnostics | Annotate incoming damage and shield use; never add another hit. |

Only non-dodged damage to enemies in active combat enters the damage-dealt totals. All reported
amounts are nonnegative integers. Source grouping uses the damage action ID, with
effect/ability IDs as fallbacks; original IDs are retained in normalized events.
Identification arriving later changes attribution of already-observed damage without
changing its amount. Friendly summons without an explicit player attribution remain
unassigned. Enemy-owned damage is shown as non-party damage.

The metric is the `DamageAmount` field without capping. In the preserved first
encounter, one resolved hit had 16 HP before, 135 damage reported, and 0 HP after.
Another had 75 HP before and 137 damage reported. Thus the value includes overkill.
No claim is made that it equals actual HP loss or excludes absorbed damage.
Additional `metrics` snapshots contain incoming damage, provider-attributed shields
and healing, and enemy-hit counts. Their counting and uncertainty rules are documented
in [support metrics](support-metrics.md); they never change damage-dealt totals.

## Persistence and recovery

Each journal event has a file-generation ID and byte offset. Events and the next
reader offset commit in one SQLite transaction. The reader checkpoints only complete
newline-terminated records, retaining partial UTF-8 lines for the next poll.
An exclusive capture lock prevents two instances from racing on a checkpoint.

The reader checks file identity, a fixed prefix, file size and bytes preceding the
checkpoint. Replacement, truncation, or a changed checkpoint starts a new generation.
On Windows it opens the log with read/write/delete sharing so it does not block the
game from writing or rotating it. These checks cannot recover records that the game
overwrote before they were captured; such sessions are not merged speculatively.

Repeated damage text is **not** a duplicate key. The fixture `repeated_procs.txt`
contains three identical 96-damage Smite broadcasts at the same timestamp, while
the adjacent HP records decrease to 5702, 5606, and 5510. All three count.

The normalized journal has schema version 2. Unsupported future database versions
are rejected without modification. Malformed recognized event records create a
visible diagnostic counter; raw lines and account credentials are not journaled.

Parser version 6 can enrich an existing generation from its unchanged raw prefix.
Every existing event's kind, timestamp and data must still agree before any enrichment
commits. Extra fields and previously ignored records are inserted atomically, ordered
by generation and original byte offset. Damage records and capture checkpoints remain
intact. An incompatible prefix declines enrichment; live capture continues. A rotated
generation without raw input retains its original totals and has unavailable components.
Version 4 adds absolute resource values, turn phases, player end-turn notifications,
and unit departures for the party window's log fallback. Cooldown events carry no
values and are not extrapolated. See [party state](party-state.md).
Version 5 adds positions and cast origins so intact older logs can reconstruct
Reckless Lunge's distance bonus. Damage and support totals are unchanged.
Version 6 retains status stack amounts added/removed, plus the inspected Burn
turn-end/Incinerate and on-kill bonus proc announcements. Deferred Burn corrections
use those facts to replace an unresolved explanation in both run and encounter
aggregates, without inserting another hit or changing its amount. A correction to an
older hit does not replace the latest-hit panel. Replaying the ordered journal after
a restart also restores pending corrections across a partially written decay record.
Parser version 9 retains both absolute values from each `ManaPoints` resource
broadcast. The reducer records a party round at each distinct `StartPlayerTurn`
and attributes a positive resource decrease to the player who spent it. Reports use
those facts for per-round values on every metric and per-Will values on damage,
healing, and shielding. The journal migration replays intact logs; it never infers a
cost from casts or from a resource increase.
Parser version 10 records the authoritative item-drafted, ability-drafted, and
ability-upgraded broadcasts. These events are attached to the next combat as
observed gear, ability, and aspect changes for encounter trends. They establish
that an addition or upgrade occurred; because broadcasts do not identify removed
items, they are not presented as a complete loadout.

Components are a separate reconstruction alongside the counting reducer. They never
replace logged damage. Reports include per-source matched/unresolved hit counts,
matched/unresolved damage, component amounts and the latest hit's inputs/prediction.
The sum of credited components plus unresolved damage equals the source total.
Integer hundredths avoid accumulated floating-point drift. See
[damage components](damage-components.md) for the accounting convention and formulas.

## Reference replay

The anonymized first-encounter fixture contains 70 accepted damage broadcasts.
Independent sums by player handle are 1303, 2634 and 1585: **5522 party damage**.
The fixture also includes player registration, class changes, repeated unit creation,
and encounter boundaries. Full local captures and reports remain ignored by Git.
