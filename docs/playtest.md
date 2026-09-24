# Windows playtest

Automated fixtures establish parsing and recovery behavior. The following checks
compare the meter against newly played combat and the user's actual display setup.

For the party window, compare each player's health/will with the game, use a binding,
then check its displayed cooldown before and after an orb or cooldown reduction.
Check a teammate ending their turn, leaving the party, and the local game restarting.
Drag the party window separately; F8/F9 should control both windows. Closing the
party window should keep capture running, and the meter/tray menu should restore it.

1. Run Inkbound in borderless/windowed mode, then launch the meter. Start a fresh run.
2. Before attacking, hover an ability over an enemy, move the targeting preview, then
   cancel. The meter must not gain damage.
3. Hit one enemy once and compare the game's displayed amount with the meter's increase.
   Use an enemy with enough HP that overkill does not confuse the comparison.
4. Repeat with a critical hit, an attack hitting multiple enemies, and burn/poison ticks.
   The appropriate player and source should receive every hit exactly once.
5. In co-op, have each player attack separately. Confirm names, attribution and shares.
6. End the encounter. Its totals must remain visible. Start the next encounter:
   encounter totals reset; whole-run totals accumulate.
7. Press F8 to lock: mouse clicks and movement should reach the game. Press F8 again
   to expand a player or move the panel. F9 should hide/show it without ending capture.
8. Restart only the meter during the run. Recorded totals must be unchanged. Restart
   the game and resume: the previous captured segment remains saved and the new segment
   must be labelled partial.
9. Check readability and click-through at the Windows display scaling you use,
   including a second monitor if applicable.

If a check fails, export the captured run and retain the local game log before
relaunching Inkbound. Note the observed amount and approximate time. Review logs
before sharing; they can contain account/network metadata unrelated to damage.

## Recorded validation

On 2026-09-22 the user confirmed a live single-attack comparison: **706 in Inkbound
and 706 added to the meter**. This validates that attack's logged damage, not every
damage mechanic or component.

Version 0.2 reconstructs 253 of 254 accepted player hits from an early preserved
snapshot, across 11 sources. Frostbite reconstructs 75 of 76 hits; the remaining
120 damage occurs during a boss phase transition and stays unresolved. The included
Frostbite fixture preserves these results. Overall damage remains 73,053 in that
snapshot.

The complete captured run contains 236,010 player damage: 525 of 534 player hits
reconstruct exactly, leaving 9,033 damage unresolved. The nine unresolved hits are
four zero hits, three phase-transition hits, and two Burn ticks with inconsistent
observed stack counts. The live upgrade preserves all 995 prior journal events,
adds stat/status history, and produces exactly the same report as a fresh replay
through its saved log position. All 46 tests pass on Linux and Windows; native
Windows hotkey, click-through, live update, and component layout checks pass.

For component playtesting, expand a player, then Frostbite, then **Last hit**. Compare
the stat inputs with your character sheet. Change one stat or vestige, trigger another
hit, and confirm the new value is used only for subsequent hits. Check the Burn
crossover row with its enabling vestige, and confirm Frostbite itself is not scaled
by the separate damage-to-Frostbitten-enemies stat.

Version 0.2.1 adds proportional component bars and a clickable **Stats at hit**
panel. Native Windows checks verify tooltip visibility while the overlay is inactive,
stats opening on click, persistence through refresh, and layout at 300-pixel width.
Visual checks also cover the default 360-pixel and maximum 700-pixel widths. Unchanged
damage rows stay alive during background log activity, preserving hover and focus.
All 52 tests pass; damage parsing and reconstruction are unchanged by this UI update.

Version 0.2.2 removes the brand row and normal status/shortcut footer. The same
360-pixel hit view is 433 pixels tall (previously 572). Capture status and run context
are available from the header indicator; capture errors remain visible. Opacity
defaults to 50%, with a percentage field and live preview in Settings. The 53-test
suite includes saving and cancelling opacity changes; native checks cover the
status tooltip, 50% window opacity, and the compact layout.

Version 0.3 adds incoming damage, provider-attributed healing and shielding,
reconstructed overhealing, unused shield accounting, and enemy-hit counts as a
pressure proxy. Automated cases include two providers shielding one recipient,
shield bypass, ambiguous lethal hits, ally healing, overheal at full HP,
prediction exclusion, between-fight support and version-2 journal enrichment.

For live validation, select **Shielded** and have one player shield another. Confirm
the provider gains the full grant. Let part of it absorb a hit and the remainder
expire; inspect the colored components. Select **Healed**, heal a nearly full ally,
and compare restored HP and overheal separately. Verify **Enemy hits** increments
for enemy hit events, including dodges; it is not a measure of targeting time.

Per-item causal attribution, reconnect merging, exclusive fullscreen rendering,
and historical comparison dashboards remain outside version 0.3.
