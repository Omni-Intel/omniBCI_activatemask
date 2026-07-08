$ErrorActionPreference = "Stop"

$Cli = "D:\arduino_IDE\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
$Config = Join-Path $env:USERPROFILE ".arduinoIDE\arduino-cli.yaml"
$Project = Split-Path -Parent $PSScriptRoot
$Sketch = Join-Path $Project "firmware\ESP32C3_ADS1299_active_mask"
$Fqbn = "esp32:esp32:esp32c3:CDCOnBoot=cdc,UploadSpeed=921600"
$Build = "D:\arduino_IDE\ArduinoBuild\ESP32C3_ADS1299_active_mask"
$Port = if ($args.Count -gt 0) { $args[0] } else { "COM3" }

New-Item -ItemType Directory -Force -Path $Build | Out-Null
& $Cli --config-file $Config compile --upload -p $Port --build-path $Build --fqbn $Fqbn $Sketch
