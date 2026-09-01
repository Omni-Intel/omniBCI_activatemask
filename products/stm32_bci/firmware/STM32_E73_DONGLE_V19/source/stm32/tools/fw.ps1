[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('doctor', 'help')]
    [string]$Command = 'help',

    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$RequiredNcsVersion = 'v3.4.0'

function Resolve-ToolPath {
    param(
        [string]$InjectedPath,
        [string]$CommandName,
        [string[]]$KnownPaths = @()
    )

    if ($InjectedPath) {
        return [IO.Path]::GetFullPath($InjectedPath)
    }

    $found = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($found) {
        return [IO.Path]::GetFullPath($found.Source)
    }

    foreach ($path in $KnownPaths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            return [IO.Path]::GetFullPath($path)
        }
    }

    return $null
}

function New-ToolStatus {
    param([string]$Path)

    $available = $Path -and (Test-Path -LiteralPath $Path -PathType Leaf)
    [ordered]@{
        available = [bool]$available
        path = if ($Path) { $Path } else { $null }
        version = if ($available) {
            (Get-Item -LiteralPath $Path).VersionInfo.ProductVersion
        } else {
            $null
        }
    }
}

function Invoke-Doctor {
    $nrfutilPath = Resolve-ToolPath `
        -InjectedPath $env:STM32_FW_NRFUTIL `
        -CommandName 'nrfutil' `
        -KnownPaths @('D:\nrfutil\nrfutil.exe')
    $jlinkPath = Resolve-ToolPath `
        -InjectedPath $env:STM32_FW_JLINK `
        -CommandName 'JLink.exe' `
        -KnownPaths @('C:\Program Files\SEGGER\JLink_V964\JLink.exe')

    $nrfutil = New-ToolStatus $nrfutilPath
    $jlink = New-ToolStatus $jlinkPath
    $report = [ordered]@{
        ready = [bool]($nrfutil.available -and $jlink.available)
        requiredNcsVersion = $RequiredNcsVersion
        nrfutil = $nrfutil
        jlink = $jlink
    }

    if ($Json) {
        $report | ConvertTo-Json -Depth 4
    } else {
        $report | Format-List
    }

    if (-not $report.ready) {
        exit 2
    }
}

switch ($Command) {
    'doctor' { Invoke-Doctor }
    default {
        Write-Output 'Usage: pwsh -File tools/fw.ps1 doctor [-Json]'
    }
}
