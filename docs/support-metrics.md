# Support and incoming damage

The provider receives shield/heal credit; the recipient receives damage-taken and
enemy-hit credit. Self-support counts. Unidentified providers remain unassigned;
an unrelated recent ability user or the healed player alone cannot establish ownership.

## Evidence inspected in build 24243

`DamageHelper.ApplyAddEnergyShield` adds the entire grant without a shield cap.
`PlayerRecordHelper.UpdatePlayerRecordsForApplyingShield` emits record `EDclDmMI`
for the provider followed by `KuFLDDDD` for the recipient. These are matched within
the same logged timestamp to `EventOnUnitGainedEnergyShield`, whose `Value` is the
grant. Records alone never add shield. Mismatched or missing recipients invalidate
the association; it is consumed once. The shield event lacks an action ID, so its
breakdown lists recipients rather than guessing a shielding ability.

Healing others emits `hkIlI6PO` / `SYuK7HPf` for the provider and `sI7wIL3j` for the
recipient before `EventOnUnitHealed`. Self-heal attribution can instead use the
inspected action graph's owner and target ports and a unique observed status caster.
When multiplayer logs omit the provider record, an `EventOnUnitPlayedAbility`
broadcast can identify the caster of that exact healing ability. The inspected
ability must directly include the logged healing action, with caster/target ports;
each wrapper can be used once per recipient. Candidates expire after two logged
seconds and clear at turn/combat/run boundaries or loss of capture continuity.
Multiple possible casters remain unassigned, including after one candidate expires.
The timestamp's last two digits are `frameCount % 100`, not fractional seconds;
wrapping from frame 97 to 03 must not invalidate the cast. Healing sources retain their action ID.
Only `RealNotPredicted`, `RealAfterPrediction` and `RealAfterMisprediction` count;
prediction and rollback broadcasts do not add healing.

`HealAmount` is already capped to missing HP. The numerical catalog extracts 58
action recipes. Graphs with several heal nodes are supported only when every heal
node has identical amount, modifier, owner and target inputs. This includes
Restoration/Mend's overheal branches and Sapper's self-heal after thread removal.
Status-count formulas use the observed provider's stacks where the graph specifies
them, covering ally-healing and end-of-combat vestiges. A partial capture cannot
establish complete stack counts for overheal reconstruction.

When the build, inputs and ownership are known,
the reducer evaluates the recipe, applies the provider's heal-increase and target's
heal-decrease stats using the game's fixed-point rounding, then checks the result
against the effective amount and missing HP. Only a matching reconstruction adds
overheal. Percent-of-maximum-HP healing uses the game's round-to-even conversion
before modifiers. A heal that finishes below maximum HP proves zero overheal
directly, even if its formula is unknown. Otherwise, unknown overheal is flagged,
not replaced by zero. An unassigned heal's
effective amount remains visible even if its attempted amount cannot be reconstructed.

The 0.4.5 replay check covers 232 healing broadcasts across eight recorded runs.
All 90 previously unassigned Restoration, Mend, ally-vestige, Sapper and
end-of-combat-vestige heals now have provider credit, including 93 recovered
overheal. Party healing rises from 127 to 526; outgoing damage, damage taken and
mitigation, shields and enemy-hit counts are unchanged. Fountains and heals
without sufficient ownership evidence remain outside player support totals.

Catalog generation from locally inspected assets is reproducible with:

```bash
python scripts/inspect_support_catalog.py --assets .local/research/assets.json \
  --output .local/support-catalog-candidate.json
```

The committed catalog contains numerical recipes and identifiers, not game assets
or decompiled source. The inspected assembly evidence is in `damage_catalog.json`.
New builds must be inspected before changing the support catalog's build number.

## Shield use and incoming damage

`ClientUnitState.TryDamageUnit` logs the resulting HP immediately before the damage
broadcast. It is context, never an additional hit. Matching requires the same target,
attacker, amount and timestamp. The reducer compares that HP with the last observed
HP/shield state and both possible paths (normal and shield-bypassing damage).
Absorption is assigned only when those paths yield one possible shield expenditure.
Lethal overkill can be ambiguous; that portion stays unresolved.

Shield grants share a pool in the game. For per-provider accounting the meter spends
grants **oldest-first (FIFO)**. This is an accounting convention, not an observed
in-game preference for one provider. A grant's bar always sums to the granted amount:

- Absorbed by a matched incoming hit.
- Active and not yet spent.
- Removed unused, including turn-boundary expiry (shown as unused / overshield).
- Unresolved when capture continuity or health evidence is lost.

Pre-existing shields at the start of a partial capture have no provider credit.
HP/shield stat resets do not create healing or grants. Grant utilization stays with
the encounter that supplied the shield even if it is used later.

## Prevention before shields (0.4.4)

Taken keeps its logged-damage total. Its bar adds **prevented** damage to the
existing health-loss/absorption components, without counting shields twice. A
fully prevented zero-damage hit still has a visible bar. Expanded rows identify
Blur, known vestige/set contributions, other reductions, and shield providers.

The incoming catalog adds 122 inspected deterministic action definitions and
recognizes a literal-damage graph that bypasses modifiers. Reconstruction shares
the outgoing arithmetic, including fixed-point scaling, increases, percent
reductions, then flat reduction and per-stack multiplication. `CalcAmount` calls
`FixedPointyExtensions.ToInt`: round to even after converting to a float, not
integer truncation. Direct-target attacks also use the observed combat-zone party
size and inspected Easy Mode multiplier. Unit enter/exit/removal events keep the
zone roster current; dead friendly units still count as the game counts them.

Only a complete formula matching the authoritative hit earns prevention credit.
This also covers zero hits when the known defenses predict zero. Missing initial
stats, party-size evidence, unsupported attacks, and mismatches remain unknown.
A `+` beside prevention means the displayed value is a known minimum. Existing
shield-absorption evidence remains useful when pre-shield prevention is unknown.

Each reduction stage earns the integer damage it removes, retaining the game's
remainder carry between percentage stages. Known additive stat contributors share
that stage proportionally, to hundredths that sum exactly. Blur receives flat
credit before Efu's Guise's extra reduction; both are capped by damage still present.
The bonus requires the vestige on the **Blur provider**. Named effects include
Thaumaturge/Warden sets, Laid Rope, Gnarled Root, and persistent equipped markers for
Port-a-Hole and Swindler's Getaway. Static item bonuses without an identifying
marker remain generic Damage reduction. Capped or offset stats that cannot be
reconciled with the active effects also remain generic. These are accounting
shares of observed prevention, not independent estimates of removing each item.

Reproduce the numerical catalog from the matching installed game and ignored local
asset inspection (requires UnityPy for a small set of English display names):

```bash
python scripts/inspect_mitigation_catalog.py --assets .local/research/assets.json \
  --game-data '/path/to/Inkbound_Data' \
  --output .local/mitigation-catalog-candidate.json
```

Assembly and asset hashes gate extraction. Parser version 8 backfills combat-zone
events and spawned-unit hitbox radii from intact logs while retaining existing
damage/support facts and offsets.
Archived captures without their raw log may lack the required context.

## Efficiency denominators

The overlay and exported reports include a per-round value for damage dealt, damage
taken, shields, healing, and enemy-hit pressure. A round is one logged
`StartPlayerTurn` for the active encounter. Damage, shields, and healing also have a
per-Will value using only the owning player's logged decreases in `ManaPoints`.
Will gained, refunds, and remaining Will are excluded. Damage taken and pressure do
not use a Will denominator because they are received metrics rather than an output
from the recipient's spend.

## Scope and enemy pressure

Incoming damage and enemy hits require active combat. Run totals include observed
heals and shields between fights, while encounter totals only include combat events.
Hub activity outside a captured run is excluded. Exact repeated broadcasts count
separately, including at identical timestamps; offsets provide persistence identity.

Enemy hits require an explicitly identified enemy source, an ability, and no ongoing
status-effect source. Dodges still count as a hit attempt. Area hits can count for
more than one player, and multi-hit abilities produce multiple events. This is
explicitly labeled **Enemy hits**, not true aggro: `EventOnAIAggro` only identifies
an enemy and combat zone, and intent broadcasts do not name the targeted player.

Parser version 3 enriches intact version-2 history atomically through the existing
prefix checks. It preserves all previous normalized fields and logged damage.
Rotated histories without the raw log cannot recover support details.
