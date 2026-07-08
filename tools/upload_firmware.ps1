. "$PSScriptRoot\common.ps1"

$Cli = Ensure-ArduinoCli
$Config = Ensure-ArduinoConfig
$Port = if ($args.Count -gt 0) { $args[0] } else { Get-DefaultSerialPort }

if (-not $Port) {
    throw "No serial port found. Plug in the ESP32-C3 or pass a port, for example: .\tools\upload_firmware.ps1 COM4"
}

Ensure-Dir $FirmwareBuildPath

Write-Host "Using Arduino CLI: $Cli"
Write-Host "Upload port: $Port"
& $Cli --config-file $Config compile --upload -p $Port --build-path $FirmwareBuildPath --fqbn $Fqbn $SketchPath
