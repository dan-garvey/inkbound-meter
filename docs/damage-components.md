# Damage component reconstruction

The meter now keeps the observed stat and status state at each damage broadcast and
reconstructs the hit independently. Expand a player, then a damage source to view
components for the selected encounter/run, or inspect its last hit. Damage totals
always use the game's broadcast amount.

## What was inspected

The installed game is Unity Mono, client build **24243** (Steam build 24849413).
Read-only inspection used `Assembly-CSharp.dll` and
`StreamingAssets/SharedScriptableObjects/SharedScriptableObjects.fb`. Relevant types
are `DamageHelper`, the direct/indirect damage nodes, `StatHelper`, `StatDataGuids`,
`UnitState`, and the serialized action graphs. The catalog records both file hashes.
No game files, process memory, or network traffic are changed or hooked.

The assembly was inspected with [ILSpy](https://github.com/icsharpcode/ILSpy).
Decompiled code and the full extracted game assets stay in ignored local research
files. The repository contains numerical definitions, identifiers, and our formula
interpreter. `scripts/inspect_damage_catalog.py` reads the installed bundle using
the inspected FlatBuffer/SoBinary layout and reproduces a catalog candidate:

```bash
python scripts/inspect_damage_catalog.py \
  --game-data '/path/to/Inkbound/Inkbound_Data' \
  --output .local/catalog-candidate.json
```

This developer command checks the approved assembly hash. A different assembly
requires inspecting its formula code before supporting it. The running meter checks
the client build marker in the log; it does not need a decompiler or asset extractor.

## Frostbite

The inspected Frostbite action has base damage **50**, Magic and Frostbite tags,
and an indirect flat-damage node. Its pre-multiplier amount is:

```
50 + Frostbite damage stat
   + Burn damage stat, when the caster has InspirationOfFlame's status
```

The outgoing percentage bucket is:

```
100 + max(0, general damage %) + max(0, Magic damage %) + max(0, Omni damage %)
```

Each point of Magic/Omni adds one percentage point here. Frostbite's flat damage
stat is added before that bucket. Frostbite procs cannot crit and do not use the
binding-only, damage-to-status, or incoming-vulnerability increases. Target damage
reduction/resistance and outgoing reduction can still apply. Frostbite consumes a
stack per proc; its damage is **not multiplied by the remaining Frostbite stacks**.

Selected stat identities from the installed data:

| Stat | Log GUID | Role |
| --- | --- | --- |
| Frostbite damage | `GxdpGRgP` | Flat addition before outgoing bonuses |
| Burn damage | `vBPDze3C` | Conditional flat addition to Frostbite |
| Magic damage | `K3G3pgjn` | Additive outgoing percentage |
| Physical damage | `zO4KuGPK` | Outgoing percentage for Physical-tagged actions |
| Omni damage | `kpnF2coo` | Outgoing percentage for matching damage tags |
| General damage multiplier | `pvw9uUnH` | Additive outgoing percentage |
| Damage to Frostbitten enemies | `hfVI0nDK` | Separate target-status multiplier for direct hits |

A recorded late Frostbite hit reconstructs as:

```
(50 base + 50 Frostbite + 13 Burn crossover) × (1 + 2.07 Omni + 0.35 Magic)
= 386.46 → 387 damage
```

Its contribution rows are 50 base, 50 Frostbite, 13 crossover, 233.91 Omni,
39.55 Magic, and 0.54 rounding. An earlier recorded hit used 50 base, 20 Frostbite,
28 Omni and 15 Magic: 100.10 rounded to 101.

## Other damage and rounding

The catalog currently contains 135 conservatively mapped action definitions. Simple
direct and indirect graphs support constant amounts, stat additions, integer
multiplication, integer division by 100, and status-count scaling. Unknown control
flow or graph nodes are excluded. Frostbite/Burn's cross-stat status branches and
Burn/Poison/Bleed's caster-specific stack lookup are mapped explicitly. Smite can
include its target's flat Smite damage stat.

### Burn state recovery (0.4.3)

Client broadcasts follow animation queues and can arrive in a different order from
the game simulation. In the latest multiplayer capture, this affects Burn stack
counts and the vestige that gains Burn damage when a burning enemy dies.

The inspected `Burn_StatusEffect` turn-end action list runs `Burn_Damage_Action`
followed by `RemoveFifthStacksStatusEffect_Action`. `StatusEffectContext.RemoveStatusEffect`
records the actual stacks removed and the remaining count. Their sum recovers the
pre-decay count without deriving it from the observed damage. A pending tick requires
an observed turn-end proc, a unique caster-specific instance, and a matching removal
in the same timestamp batch. The removal must match the graph's fifth-of-stacks rule
(at least one stack). Another application, an ambiguous intervening hit, a changed
phase, a missing delta or an Incinerate proc prevents this recovery. Other players'
instances are kept separate.

`VestigeAll_Epic_BurnDamageOnBurn_Action` adds exactly one Burn damage to its source's
equipment. A proc already announced before a tick can be included once its matching
positive stat update confirms it. Other hit inputs stay frozen. A kill caused by the
current hit does not boost that hit. Unconfirmed, contradictory or reordered updates
remain unresolved; future stats are not searched for a value that fits.

`DamageHelper.ApplyDamage` clamps damage against units with an explicit near-death HP
threshold (`frNtAO1z`). Kraken tentacles start with a threshold of 1. For a reconstructed
Burn tick, known pre-hit health and shield bound the final amount. The reduction is
shown as **Minimum health limit** so the components still sum to the logged damage.
Ordinary overkill is not clamped. This does not model boss phase transitions that
clear status instances before the damage broadcast.

Validation used one frozen 122,840,887-byte capture (overlapping the earlier snapshots):

| Burn | Before | After |
| --- | ---: | ---: |
| Matched hits | 103 / 142 | 136 / 142 |
| Matched damage | 343,049 | 613,577 |
| Unresolved damage | 338,797 | 68,269 |
| Component coverage | 50.3% | 90.0% |

This recovers **270,528 damage across 33 ticks**. The six remaining ticks comprise
14,354 damage at a boss phase transition and 53,915 with inconsistent stat broadcast
ordering. Across all 4,803 party hits, damage and support reports are identical when
component explanations are excluded; no previously matched hit regresses. There are
no parse errors. All 39 initially unresolved Burn ticks are preserved as numerical
regression fixtures, including the six that must remain unresolved. Tests also cover
same-caster ambiguity, ownership, confirmation timing, overkill, duplicate records,
latest-hit preservation, split writes, restart and v5 history enrichment.

The extractor follows inspected control connections and excludes disconnected
damage nodes. Branches and repeated strikes are supported when every reachable
damage node has the same formula. Each logged strike remains one hit: Throw's
double-cast branch does not double each broadcast again. This also covers Cleave
and its ascensions, Throw/Poison Shot, and Infused Fist's collision victims.
Chi Eruption's physical component includes its conditional combo bonus. Spiked
uses the holder's stats and its own unambiguous status instance, including shields
or statuses supplied by another player; missing initial stats still prevent credit.

Reckless Lunge's physical and magic graphs add **25 damage per whole unit of
distance** before outgoing bonuses. The logs provide unit spawn, movement,
teleport and cast-origin positions. Parser version 5 captures those coordinates
on the game's 1/4096 grid, and the interpreter reproduces its integer square-root
rounding before flooring distance. A matching cast origin is required in the
current combat phase. This is separate from the generic segmented ability-distance
multiplier. Parser version 8 also preserves the spawned target's hitbox radius for
scaled radial abilities. Whirlwind has two custom rings across its five-unit width:
the game applies 100% inside the 80% edge and 200% at/after that edge, after crit
and before reductions. Ring membership uses the target edge, the game's fixed-point
square root, and the exact cast origin. A same-tick triggered effect can publish its
own cast record before the Whirlwind damage broadcast; that broadcast remains the
authoritative Whirlwind association for range scaling. The meter labels the additional portion
**Scaled range**. If position or hitbox history is absent, a hit still receives
component credit when its observed amount proves the 100% ring; potential boosted
hits remain unresolved. Both inspected Reckless Lunge graphs read
the magic-damage upgrade stat, including the physical-tagged graph.

Existing journals are enriched from intact logs on upgrade, retaining all original
damage event identities and amounts. Rotated logs cannot provide missing positions
or hitbox radii.

Zero-damage broadcasts are excluded from damage rows and component hit counts. In
the captured client log these non-dodged zeroes are immunity or phase-state results,
not damage dealt.

Divine Touch's on-hit Smite proc is a separate indirect action. Its inspected graph
deals **50 + the status holder's Smite damage** to the context target; it therefore
appears as its own source row rather than being folded into the normal Smite action.

Direct hits additionally apply binding multipliers, applicable global/ability
status bonuses, target vulnerability/weakness, and crit damage. The game computes
some multiplier percentages using fixed-point arithmetic (4096 units per one).
The interpreter reproduces those percentage calculations and carries integer
hundredths through the damage stages. Increases round up once; reductions then
truncate. Burn, Poison and Bleed multiply the resulting per-stack integer damage
by the caster's stack instance **after rounding**, not before it.

## Meaning of contribution credit

This is a calculation-order waterfall, not a counterfactual item-removal model.
Base and source-specific scaling are credited first. Outgoing bonuses receive
their additions over the complete base amount. Later multipliers receive their
increase over the preceding stages; reductions are negative rows. Interactions are
credited to the later input/stage exactly once. Rounding is explicit. Nonlinear
source expressions use their serialized input order to allocate interaction credit.

Only complete reconstructions equal to the logged amount enter component totals.
The last-hit view may show an unmatched prediction, clearly marked as excluded.
Every source total remains matched damage plus unresolved damage, including hits
whose components cannot be reconstructed at all.

## Known limits

- Initial defaults require a full new-run capture. Party peers announced as
  resumed use their inspected immutable unit/class baseline, while observed stat
  broadcasts replace it; an exact logged-hit match is still required. Reconnects
  without the run start, old journals without recoverable stat history, and
  unknown builds retain damage totals but do not claim component attribution.
- Broadcast order can differ from calculation order near phase changes or proc
  chains. In particular, a boss phase transition can remove the originating DoT
  before emitting its final positive damage broadcast. Stats captured at the
  broadcast are not a guaranteed simulation snapshot.
- Boss phase HP clamps, immunity, direct-target difficulty scaling, segmented distance multipliers,
  and unsupported action graphs remain unresolved. Identical totals alone cannot
  prove every hidden input was captured; “reconstructed” means formula-matched.
- Multiple possible stack instances for the same caster/effect are ambiguous and
  remain unresolved. Other players' DoT stacks are never added to this caster's count.
- Shocked's propagation depends on the triggering hit and proc context. That causal
  chain is not reconstructed yet. Discharge and some DoT ticks also have unresolved
  differences between state at calculation time and state at the damage broadcast.
- Component attribution explains stat categories and modeled interactions. It does
  not yet assign each stat point to a specific vestige, augment, or other item.

Version 0.4.2 was checked against a frozen five-run replay containing 1,696 party
hits and 847,410 damage. Formula-matched damage increased from 615,547 to 753,995
(72.6% to 89.0%); matched hits increased from 1,050 to 1,437. No previously matched
hit regressed, and every run/player/source total and support metric stayed identical.

| Newly reconstructed source | Additional matched damage |
| --- | ---: |
| Reckless Lunge, physical and magic | 58,573 |
| Chi Eruption, physical | 26,655 |
| Infused Fist collisions | 19,982 |
| Throw and Poison Shot | 22,220 |
| Cleave and Hemorrhage | 11,018 |
| **Total recovered** | **138,448** |

The remaining 93,415 damage is kept unresolved: 27,254 from unmapped Shocked
propagation, 42,469 from formula/state mismatches, 11,094 from missing/ambiguous DoT
instances, and 12,598 from missing initial player stats. Zero/immune hits are also
left unresolved. No hidden stat is inferred by fitting the observed hit.

Regression fixtures contain numerical state for sixteen real hits across the eight
newly matched sources. Additional checks cover double casts, conditional combo
ownership, disconnected graph branches, Spiked ownership and stack ambiguity,
fixed-point distance boundaries, missing cast positions, and v4 journal enrichment.
The earlier 76-hit Frostbite excerpt still reconstructs 75 hits and preserves its
phase-transition mismatch. All 107 tests pass on Linux and Windows; the package also
checks native Windows controls and layout.

A second, newer 101 MB snapshot contains 3,139 party hits and 1,111,627 damage.
Matched damage increases from 832,349 to 981,922 (74.9% to 88.3%), recovering 149,573
damage and 494 hits. It also has no previously matched hit regressions, no parse
errors, and unchanged damage and support totals. These snapshots overlap and their
recovered amounts must not be added together.
