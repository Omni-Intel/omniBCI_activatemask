$ErrorActionPreference = "Stop"

$ideCommand = Get-Command "Arduino IDE.exe" -ErrorAction SilentlyContinue
if (-not $ideCommand) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Arduino IDE\Arduino IDE.exe",
        "$env:ProgramFiles\Arduino IDE\Arduino IDE.exe",
        "${env:ProgramFiles(x86)}\Arduino IDE\Arduino IDE.exe"
    )
    $ide = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
} else {
    $ide = $ideCommand.Source
}

if (-not $ide) {
    throw "Arduino IDE was not found. Install Arduino IDE 2.x, or use tools\compile_firmware.ps1 with arduino-cli."
}

$project = Split-Path -Parent $PSScriptRoot
$localAppData = Join-Path $project ".arduino\LocalAppData"
$temp = Join-Path $project ".arduino\Temp"
$sketch = Join-Path $project "firmware\ESP32C3_ADS1299_active_mask\ESP32C3_ADS1299_active_mask.ino"

New-Item -ItemType Directory -Force -Path $localAppData, $temp | Out-Null

$env:LOCALAPPDATA = $localAppData
$env:TEMP = $temp
$env:TMP = $temp

Start-Process -FilePath $ide -ArgumentList "`"$sketch`"" -WorkingDirectory (Split-Path -Parent $ide)
