$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $PSScriptRoot "setup_pc_env.ps1")
}

& $Python -m pip install pyinstaller
Push-Location (Join-Path $Project "pc_app")
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name "ActiveMaskGUI" `
        --distpath (Join-Path $Project "dist") `
        --workpath (Join-Path $Project "build\pyinstaller") `
        --specpath (Join-Path $Project "build\pyinstaller") `
        "active_mask_gui.py"
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Packaged GUI:"
Write-Host (Join-Path $Project "dist\ActiveMaskGUI\ActiveMaskGUI.exe")
