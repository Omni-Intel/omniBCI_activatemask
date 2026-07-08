. "$PSScriptRoot\common.ps1"

$Cli = Ensure-ArduinoCli
$Config = Ensure-ArduinoConfig

Write-Host "Using Arduino CLI: $Cli"
Write-Host "Using Arduino config: $Config"
Write-Host "Installing ESP32 Arduino core into: $ArduinoData"

& $Cli --config-file $Config core update-index
& $Cli --config-file $Config core install esp32:esp32
& $Cli --config-file $Config core list
