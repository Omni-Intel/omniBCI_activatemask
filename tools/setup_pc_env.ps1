$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Project ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Requirements = Join-Path $Project "pc_app\requirements.txt"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Creating project-local Python venv..."
    py -3 -m venv $Venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r $Requirements
Write-Host "Python environment ready: $Python"
