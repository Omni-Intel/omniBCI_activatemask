$ErrorActionPreference = "Stop"

$Cli = "D:\arduino_IDE\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
$Config = Join-Path $env:USERPROFILE ".arduinoIDE\arduino-cli.yaml"
$Data = "D:\arduino_IDE\Arduino15_data"
$Url = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"

New-Item -ItemType Directory -Force -Path $Data | Out-Null
& $Cli --config-file $Config config set directories.data $Data
& $Cli --config-file $Config config add board_manager.additional_urls $Url
& $Cli --config-file $Config core update-index
& $Cli --config-file $Config core install esp32:esp32
& $Cli --config-file $Config core list
