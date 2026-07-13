. "$PSScriptRoot\common.ps1"

$Cli = Ensure-ArduinoCli
$Config = Ensure-ArduinoConfig
$Sketch = Resolve-SketchPath $(if ($args.Count -gt 0) { $args[0] } else { $null })
$Build = Resolve-BuildPath $Sketch

Ensure-Dir $Build

Write-Host "Using Arduino CLI: $Cli"
Write-Host "Sketch: $Sketch"
Write-Host "Build path: $Build"
& $Cli --config-file $Config compile --build-path $Build --fqbn $Fqbn $Sketch
