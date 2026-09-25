# Inkbound Meter

A local Windows overlay that reads Inkbound's combat log. It shows damage dealt,
damage taken, shields and healing provided, and enemy hits received, with player
shares and breakdowns for the current encounter or whole run. It works alongside
borderless/windowed Inkbound. A separate party window shows current health, shield,
will and binding cooldowns using read-only access to the running game's state.

## Run on Windows

Extract **InkboundMeter-windows.zip** and run **InkboundMeter.exe** inside its folder.
Keep the `_internal` folder beside the executable. No Python installation is needed
for the packaged app. Start Inkbound normally; the meter locates its log automatically.

- **F8:** switch both windows between interaction and click-through. They start unlocked.
- **F9:** show or hide both windows.
- The **Party** window moves independently: drag its header while unlocked.
  Health bars include current/maximum HP and a blue shield strip; purple pips show will.
  Binding tiles show turns remaining or **✓** when off cooldown. Hover for full binding
  names and details. A check beside a player means they ended their turn; a cross means
  they are downed. Gold tile borders indicate a guaranteed critical hit.
- Use **··· → Party overlay** (also in the tray menu) to toggle the party window.
  Its **×** hides just that window. Positions and visibility are saved separately;
  opacity and width settings apply to both windows.
- Click **Damage ▾** in the header to choose a statistic. **Shielded / Healed**
  credit the provider, including self-support; **Taken / Enemy hits** credit the recipient.
- While unlocked, drag the header, click a player to expand their damage sources,
  then click a source for **Total components / Last hit**. Switch between
  **Current encounter / Whole run** to change the aggregation scope.
- Component bars use consistent colors and proportional widths. Expand a source
  for its color legend, exact damage, and shares. In **Last hit**, click
  **Stats at hit** to open the recorded inputs; they stay open as new hits arrive.
- The **···** menu contains settings, JSON export, and Exit. Opacity, width,
  placement, scope, statistic, and shortcuts are saved.
- **Settings → Opacity** has a slider and percentage field, with a live preview.
  The default is **50%**. Save keeps the change; Cancel restores the saved opacity.
- Hover the header's status dot for capture status and run context. Hover the menu
  for shortcut hints. Capture errors remain visible when attention is needed.
- The system tray icon can show and unlock the overlay if a shortcut conflicts.

The default log is `%USERPROFILE%\AppData\LocalLow\Shiny Shoe\Inkbound\logfile.log`.
Settings and the event journal are stored in `%LOCALAPPDATA%\InkboundMeter`.
To use a different log, launch `InkboundMeter.exe live --log "C:\path\logfile.log"`.

## Sharing

Send the Windows zip from `dist` with the whole app folder inside. The recipient
extracts it and runs `InkboundMeter.exe`; `START_HERE.txt` has the short setup guide.
The archive includes the required runtime. Each user keeps separate settings and
capture history on their own computer.

## What the numbers mean

**Damage is the game's logged resolved damage amount, including overkill.**
It is not a reconstruction of HP removed. Party percentages use the selected
statistic's attributed party total. Damage dealt excludes damage to friendlies,
events outside active combat, and dodged attacks.

- **Taken:** logged incoming combat damage, split into HP lost, shield absorption,
  overkill and unresolved amounts where matching health evidence is available.
- **Shielded:** all shield granted by a player, including unused shielding.
  Its colored bar separates absorbed, active and removed unused shield (overshield).
  When grants overlap, the shared shield pool is spent oldest-first for attribution.
- **Healed:** effective HP restored plus reconstructed overhealing. Only inspected
  formulas matching observed healing and known stats contribute overheal. Missing
  formulas/state remain explicitly unavailable. Provider identity comes from
  provider/recipient records, matching healing casts, or inspected effect ownership; ambiguous providers
  and environmental healing remain outside player totals.
- **Enemy hits:** resolved enemy attack hit events, including dodges and excluding
  ongoing effects. This is a pressure proxy: the log does not expose targeting
  history or threat, and area attacks may hit several players.

Healing and shielding between fights count in **Whole run**, not the last encounter.
Expand a player for component amounts, healing sources and support recipients.
See [support counting details](docs/support-metrics.md) for reconstruction boundaries.

Each player row also shows efficiency from captured game state: every metric has an
amount per recorded party round, and damage, healing, and shielding additionally
show an amount per Will actually spent by that player. A round is one
`StartPlayerTurn` broadcast. The Will denominator sums only observed `ManaPoints`
decreases, so gained, refunded, and unspent Will do not dilute the figure. Damage
taken and enemy-hit pressure have no per-Will figure because another player's
spending is not a meaningful denominator for them.

Only `EventSystem` damage broadcasts count. Simulation messages, client HP debug
lines, and subscriber messages do not add damage. Equal hits at the same timestamp
remain separate hits. Burn, poison, and other procs are grouped by their logged
damage action, with readable names where possible.

Source components reconstruct base damage, source-specific bonuses, Omni/Magic/Physical
damage, binding and target bonuses, critical hits, reductions, and rounding from
the stats observed at each hit. Frostbite also tracks the vestige interaction that
adds Burn damage. **Only reconstructions that exactly match the logged hit enter
component totals.** Unresolved damage remains in the damage total and has its own row.
The last-hit view shows the recorded value, with mismatched predictions marked and
hatched. Open **Stats at hit** for the inputs. Gray hatching represents unresolved
damage. Negative contributions appear in red below the main bar; both lanes use the
same scale, and their legend shares use damage before reductions. These are
contributions in calculation order, so interaction damage
is credited to the later modifier, not a claim about damage lost by removing an item.

Version 0.4.12 maps 138 outgoing action definitions from client build **24243**, including
Frostbite, Burn, Poison, Bleed, Smite and many bindings. It does not yet cover every
source or boss phase behavior. An unknown build, incomplete initial
stats, or a mismatched hit is explicit. See [the formula notes](docs/damage-components.md).
The catalog also covers Chi Eruption, Infused Fist collisions, Throw/Poison Shot,
Cleave and Reckless Lunge's distance bonus. Upgrades backfill recorded positions
from intact logs to improve breakdowns for earlier runs; damage totals stay unchanged.
Whirlwind now shows its inspected scaled-range contribution when the log contains
the cast position and target hitbox radius, including when a same-tick triggered
effect follows the cast before the damage broadcast.
Authoritative zero-damage broadcasts from immunity or phase states are excluded
from the meter, since they do not represent dealt damage.
Divine Touch's Smite status proc is mapped separately from the normal Smite action:
it contributes its 50 base damage plus the holder's Smite damage to each target.
Legendary Vestige Smite Again uses that same 50-plus-holder-Smite formula with its
Magic tag. Damage clipped by a linked enemy's 1 HP near-death floor is shown as its
own negative component rather than left unresolved.
Party members announced as resumed now receive their inspected class baseline when
the run start is present in the log, so their exact matching component coverage no
longer starts at zero. Legendary Shield Bash Headbutt also records its current-shield
base component when the hit can be reconstructed exactly.
Burn now recovers stale stack counts from its recorded turn-end decay, confirms delayed
on-kill damage bonuses, and accounts for the 1 HP limit on Kraken tentacles. These
corrections also apply to saved runs when the original log is still available.
On the latest replay, Burn component coverage rises from 50.3% to 90.0%; ambiguous
stat ordering and unsupported boss phase transitions remain unresolved.

**Taken** now colors the bar by health loss, shield absorption, Blur, and identified
vestige/set reductions. It reconstructs 122 additional incoming attack definitions.
The headline still counts logged damage; **prevented** damage is shown alongside it.
Expand a player for named mitigation components and the providers of absorbed shields.

Open **Encounter trends…** from the meter menu to compare each encounter's party
output per round for damage, incoming damage, shielding, healing, or enemy hits.
Hover a bar for its denominator and the changes observed before that fight. The
change lane reports only authoritative draft broadcasts (gear, abilities, and
aspects); it records additions and upgrades, not a reconstructed complete loadout
or unequips the log does not identify.
Only matching reconstructions receive prevention credit. A `+` marks a known minimum
when some hits cannot be reconstructed. Unidentified item bonuses remain **Damage
reduction**, and incomplete history stays explicitly unknown.

Enemy self-damage and linked damage between boss parts are listed separately;
the meter does not guess which player caused them. Unknown actors remain unassigned
until identified. Unrecognized class IDs are displayed unchanged.
In live mode, names appear before the first cast when the memory reader's run seed
and full party roster match the log. This does not change saved reports. Expanding
damage details keeps the window inside the display's work area; longer lists scroll.

The last fight remains visible between encounters. Runs are separated by connection
boundaries. A run is marked complete from the start only when the log includes an
explicit new-run creation or new-run vote event. Resuming/reconnecting without that evidence starts
a **partial capture**, rather than claiming earlier damage or merging by seed.
Restarting the meter preserves already-captured events without counting them again.
Logs truncated or replaced by the game start a new file generation.
Upgrades enrich an intact current log with stat/status and support history.
Previously captured damage is verified and preserved; older rotated logs cannot
supply missing details and are marked accordingly.

All data stays on this computer. The app reads the combat log for historical totals
and reads game memory for the live party window. It does not modify the game, install
a mod, inject code, or contact a server. The memory reader supports inspected client
build **24243**; unsupported versions fall back to logged health/will and explicitly
unavailable cooldowns. Replay and headless capture use logs only. Live memory snapshots
are not saved in the journal or exported combat reports. See [party state](docs/party-state.md).
Exported reports include observed player
display names; review them before sharing. Full original logs are not stored in the
meter's journal.

## Development

Use Python 3.11 or newer. The parser, reducer and replay command are independent of
the UI. The Windows executable must be built on Windows.

```bash
python -m venv .venv
# Activate your environment, then:
python -m pip install -e ".[dev,build]"
python -m pytest -q
ruff check .
python -m inkbound_meter demo
python -m inkbound_meter replay path/to/logfile.log --output report.json
python -m inkbound_meter replay path/to/logfile.log --gui
python -m inkbound_meter live
```

For a headless reader, use `live --headless`; `live --once` reads through the latest
complete line, emits JSON and exits. Use a separate `--data-dir` for experiments.
Only one live capture may use a data directory at a time. A saved-log replay does
not write to the live journal.

From Windows PowerShell, package and test the app:

```powershell
.\scripts\build_windows.ps1
```

The script creates `dist\InkboundMeter\InkboundMeter.exe` and
`dist\InkboundMeter-windows.zip`. Its build environment lives outside the checkout,
under `%LOCALAPPDATA%\InkboundMeter\build`. It also supports a checkout accessed
through WSL's `\\wsl.localhost\...` path.

Raw captures, local reports, virtual environments, databases and build output are
Git-ignored. Tests use anonymized event excerpts. See [the data contract](docs/data-contract.md)
and [the playtest checklist](docs/playtest.md) for accuracy boundaries and validation.

## References

The older [Selliott452 community meter](https://github.com/Selliott452/inkbound-damage-meter)
provided useful examples of Inkbound's log format. This project is a fresh implementation.
Window behavior follows [Qt's window flags](https://doc.qt.io/qtforpython-6/PySide6/QtCore/Qt.html)
and [Windows global hotkeys](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).
