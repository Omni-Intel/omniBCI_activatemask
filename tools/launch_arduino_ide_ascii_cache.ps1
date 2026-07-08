$ErrorActionPreference = "Stop"

$ide = "D:\arduino_IDE\Arduino IDE\Arduino IDE.exe"
$localAppData = "D:\arduino_IDE\LocalAppData"
$temp = "D:\arduino_IDE\Temp"

New-Item -ItemType Directory -Force -Path $localAppData, $temp | Out-Null

$env:LOCALAPPDATA = $localAppData
$env:TEMP = $temp
$env:TMP = $temp

Start-Process -FilePath $ide -WorkingDirectory (Split-Path -Parent $ide)
