$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
if ($env:OS -ne "Windows_NT") { throw "Use this launcher on Windows." }
if (-not (Test-Path .venv-win\Scripts\python.exe)) {
    py -3.12 -m venv .venv-win
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12 x64 with Tcl/Tk and the Python launcher." }
}
& .\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
& .\.venv-win\Scripts\python.exe main.py
if ($LASTEXITCODE -ne 0) { throw "Application failed to start." }
