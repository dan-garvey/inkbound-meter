param(
    [string]$Python = "py",
    [string]$BuildRoot = (Join-Path $env:LOCALAPPDATA "InkboundMeter\build")
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $BuildRoot "venv"
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    if ($Python -eq "py") { & $Python -3.11 -m venv $Venv }
    else { & $Python -m venv $Venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Windows build environment." }
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install -e "${Repo}[dev,build]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
Push-Location $Repo
try {
    & $VenvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Windows tests failed." }
    # Native hit testing and screen placement need the real Windows Qt platform.
    $PreviousPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = "windows"
        & $VenvPython -m pytest -q `
            tests/test_overlay.py::test_settings_arrow_clicks_reach_both_spinboxes `
            tests/test_overlay.py::test_multiplayer_growth_stays_inside_screen_and_all_players_scroll_into_view `
            tests/test_overlay.py::test_taken_bar_shows_blur_set_and_shield_without_inflating_total `
            tests/test_overlay.py::test_healed_view_credits_multiplayer_caster_with_restored_and_overheal_bar
        if ($LASTEXITCODE -ne 0) { throw "Native Windows settings, layout, or support view failed." }
    } finally { $env:QT_QPA_PLATFORM = $PreviousPlatform }
    & $VenvPython -m PyInstaller --noconfirm --clean --windowed --name InkboundMeter `
        --collect-data inkbound_meter `
        --paths (Join-Path $Repo "src") --distpath (Join-Path $Repo "dist") `
        --workpath (Join-Path $BuildRoot "pyinstaller") --specpath $BuildRoot `
        (Join-Path $Repo "launcher.py")
    if ($LASTEXITCODE -ne 0) { throw "Packaging failed." }
    Copy-Item (Join-Path $Repo "README.md") (Join-Path $Repo "dist\InkboundMeter\README.md")
    Copy-Item (Join-Path $Repo "docs\quick-start.txt") (Join-Path $Repo "dist\InkboundMeter\START_HERE.txt")
    Copy-Item (Join-Path $Repo "docs") (Join-Path $Repo "dist\InkboundMeter\docs") -Recurse -Force
    Compress-Archive -Path (Join-Path $Repo "dist\InkboundMeter") `
        -DestinationPath (Join-Path $Repo "dist\InkboundMeter-windows.zip") -Force
    Write-Host "Built: $(Join-Path $Repo 'dist\InkboundMeter\InkboundMeter.exe')"
} finally { Pop-Location }
