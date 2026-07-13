. "$PSScriptRoot\common.ps1"

$Cli = Ensure-ArduinoCli
$Config = Ensure-ArduinoConfig
$Port = if ($args.Count -gt 0) { $args[0] } else { Get-DefaultSerialPort }
$SketchArg = if ($args.Count -gt 1) { $args[1] } else { $null }
$Sketch = Resolve-SketchPath $SketchArg
$Build = Resolve-BuildPath $Sketch

if (-not $Port) {
    throw "No serial port found. Plug in the ESP32-C3 or pass a port, for example: .\tools\upload_firmware.ps1 COM4"
}

Ensure-Dir $Build

Write-Host "Using Arduino CLI: $Cli"
Write-Host "Upload port: $Port"
Write-Host "Sketch: $Sketch"
& $Cli --config-file $Config compile --upload -p $Port --build-path $Build --fqbn $Fqbn $Sketch
