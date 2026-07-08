. "$PSScriptRoot\common.ps1"

$Cli = Ensure-ArduinoCli
$Config = Ensure-ArduinoConfig

Ensure-Dir $FirmwareBuildPath

Write-Host "Using Arduino CLI: $Cli"
Write-Host "Build path: $FirmwareBuildPath"
& $Cli --config-file $Config compile --build-path $FirmwareBuildPath --fqbn $Fqbn $SketchPath
