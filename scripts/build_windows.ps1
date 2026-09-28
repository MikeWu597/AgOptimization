param([switch]$SkipGuiTest)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)" }
}

if ($env:OS -ne "Windows_NT") { throw "Build this package on Windows, not macOS." }
# Install Python 3.12 x64 from python.org, with Tcl/Tk and the Python launcher.
py -3.12 -m venv .venv-win
Check-Exit "Create venv"
$Python = Join-Path $ProjectRoot ".venv-win\Scripts\python.exe"
& $Python -m pip install -r requirements-build.txt
Check-Exit "Install dependencies"
& $Python -m unittest discover -v
Check-Exit "Tests"
if (-not $SkipGuiTest) {
    & $Python -m tests.gui_smoke
    Check-Exit "GUI smoke test"
}
& $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name MinecraftRegionCleaner --collect-all lz4 --collect-all xxhash main.py
Check-Exit "Build"
$Exe = Join-Path $ProjectRoot "dist\MinecraftRegionCleaner\MinecraftRegionCleaner.exe"
$Smoke = Start-Process -FilePath $Exe -ArgumentList "--smoke-test" -Wait -PassThru
if ($Smoke.ExitCode -ne 0) { throw "Packaged application startup check failed." }
Copy-Item README.md dist\MinecraftRegionCleaner\README.md
Compress-Archive -Path dist\MinecraftRegionCleaner -DestinationPath dist\MinecraftRegionCleaner-windows-x64.zip -Force
Write-Host "Ready: dist\MinecraftRegionCleaner-windows-x64.zip"
Write-Host "Distribute the complete folder, including _internal."
