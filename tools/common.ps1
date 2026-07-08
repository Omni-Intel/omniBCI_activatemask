$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LocalTools = Join-Path $ProjectRoot ".local_tools"
$ArduinoRoot = Join-Path $ProjectRoot ".arduino"
$ArduinoData = Join-Path $ArduinoRoot "data"
$ArduinoUser = Join-Path $ArduinoRoot "user"
$ArduinoDownloads = Join-Path $ArduinoRoot "downloads"
$ArduinoCliDir = Join-Path $LocalTools "arduino-cli"
$ArduinoCliExe = Join-Path $ArduinoCliDir "arduino-cli.exe"
$ArduinoConfig = Join-Path $ArduinoRoot "arduino-cli.yaml"
$BuildRoot = Join-Path $ProjectRoot "build"
$SketchPath = Join-Path $ProjectRoot "firmware\ESP32C3_ADS1299_active_mask"
$FirmwareBuildPath = Join-Path $BuildRoot "ESP32C3_ADS1299_active_mask"
$Fqbn = "esp32:esp32:esp32c3:CDCOnBoot=cdc,UploadSpeed=921600"
$Esp32PackageUrl = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"

function Ensure-Dir {
    param([Parameter(Mandatory=$true)][string]$Path)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Get-ArduinoCli {
    if ($env:ARDUINO_CLI -and (Test-Path -LiteralPath $env:ARDUINO_CLI)) {
        return $env:ARDUINO_CLI
    }

    $command = Get-Command arduino-cli -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    if (Test-Path -LiteralPath $ArduinoCliExe) {
        return $ArduinoCliExe
    }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        "$env:ProgramFiles\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        "${env:ProgramFiles(x86)}\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        "D:\ArduinoIDE\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        "D:\arduino_IDE\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
    )
    $candidate = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($candidate) {
        return $candidate
    }

    return $null
}

function Install-LocalArduinoCli {
    $version = "1.5.1"
    Ensure-Dir $ArduinoCliDir
    $zipPath = Join-Path $env:TEMP "arduino-cli_$version`_Windows_64bit.zip"
    $extractPath = Join-Path $env:TEMP "arduino-cli_$version`_extract"
    $url = "https://downloads.arduino.cc/arduino-cli/arduino-cli_$version`_Windows_64bit.zip"

    Write-Host "Downloading Arduino CLI $version to project-local tools..."
    Invoke-WebRequest -Uri $url -OutFile $zipPath
    Remove-Item -LiteralPath $extractPath -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
    Copy-Item -LiteralPath (Join-Path $extractPath "arduino-cli.exe") -Destination $ArduinoCliExe -Force
    Write-Host "Arduino CLI installed: $ArduinoCliExe"
    return $ArduinoCliExe
}

function Ensure-ArduinoCli {
    $cli = Get-ArduinoCli
    if ($cli) {
        return $cli
    }
    return Install-LocalArduinoCli
}

function Ensure-ArduinoConfig {
    Ensure-Dir $ArduinoRoot
    Ensure-Dir $ArduinoData
    Ensure-Dir $ArduinoUser
    Ensure-Dir $ArduinoDownloads

    $yaml = @"
board_manager:
    additional_urls:
        - $Esp32PackageUrl
directories:
    data: $ArduinoData
    downloads: $ArduinoDownloads
    user: $ArduinoUser
locale: en
"@
    Set-Content -LiteralPath $ArduinoConfig -Value $yaml -Encoding UTF8
    return $ArduinoConfig
}

function Get-DefaultSerialPort {
    $ports = Get-CimInstance Win32_SerialPort -ErrorAction SilentlyContinue
    if (-not $ports) {
        return $null
    }

    $preferred = $ports | Where-Object {
        $_.DeviceID -match '^COM\d+$' -and (
            $_.Name -match 'USB|CH340|CP210|Serial|UART' -or
            $_.Description -match 'USB|CH340|CP210|Serial|UART'
        )
    } | Select-Object -First 1

    if ($preferred) {
        return $preferred.DeviceID
    }

    return ($ports | Select-Object -First 1).DeviceID
}
