$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup_pc_env.ps1")
}

Push-Location (Join-Path $Project "pc_app")
try {
    & $Python "active_mask_gui.py"
} finally {
    Pop-Location
}
