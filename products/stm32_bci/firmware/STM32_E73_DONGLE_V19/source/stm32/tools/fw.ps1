[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('doctor', 'build', 'help')]
    [string]$Command = 'help',

    [switch]$Json,

    [string]$Version,

    [string]$BuildDir,

    [string]$KeyPath,

    [switch]$DryRun
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

function New-DirectoryStatus {
    param(
        [string]$Path,
        [string[]]$RequiredChildren
    )

    $resolved = if ($Path) { [IO.Path]::GetFullPath($Path) } else { $null }
    $available = $resolved -and (Test-Path -LiteralPath $resolved -PathType Container)
    if ($available) {
        foreach ($child in $RequiredChildren) {
            if (-not (Test-Path -LiteralPath (Join-Path $resolved $child))) {
                $available = $false
                break
            }
        }
    }

    [ordered]@{
        available = [bool]$available
        path = $resolved
    }
}

function Find-ToolchainRoot {
    if ($env:STM32_FW_TOOLCHAIN_ROOT) {
        return [IO.Path]::GetFullPath($env:STM32_FW_TOOLCHAIN_ROOT)
    }

    $root = 'D:\ncs\toolchains'
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        return $null
    }
    $match = Get-ChildItem -LiteralPath $root -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'manifest.json') -PathType Leaf
    } | Select-Object -First 1
    if ($match) { return $match.FullName }
    return $null
}

function Invoke-NcsVersionProbe {
    param(
        [string]$NrfutilPath,
        [string]$NcsRoot
    )

    $probe = 'west --version & cmake --version & ninja --version & arm-zephyr-eabi-gcc --version & arm-zephyr-eabi-gdb --version & python --version & python bootloader\mcuboot\scripts\imgtool.py version & echo __GDB_PATH__ & where arm-zephyr-eabi-gdb'
    $arguments = @(
        'sdk-manager', 'toolchain', 'launch',
        '--ncs-version', $RequiredNcsVersion,
        '--install-dir', 'D:\ncs',
        '--chdir', $NcsRoot,
        '--', 'cmd.exe', '/d', '/c', $probe
    )
    $output = (& $NrfutilPath @arguments 2>&1 | Out-String)
    $succeeded = $LASTEXITCODE -eq 0
    $gdbPath = $null
    if ($output -match '(?ms)__GDB_PATH__\s*\r?\n([^\r\n]+arm-zephyr-eabi-gdb\.exe)') {
        $gdbPath = $Matches[1].Trim()
    }

    [ordered]@{
        available = [bool]$succeeded
        west = if ($output -match 'West version:\s*v?([^\s]+)') { $Matches[1] } else { $null }
        cmake = if ($output -match 'cmake version\s+([^\s]+)') { $Matches[1] } else { $null }
        ninja = if ($output -match '(?m)^(\d+\.\d+\.\d+)\s*$') { $Matches[1] } else { $null }
        gcc = if ($output -match 'arm-zephyr-eabi-gcc[^\r\n]*\)\s+([^\s]+)') { $Matches[1] } else { $null }
        gdb = if ($output -match 'GNU gdb[^\r\n]*\)\s+([^\s]+)') { $Matches[1] } else { $null }
        gdbPath = $gdbPath
        python = if ($output -match 'Python\s+([^\s]+)') { $Matches[1] } else { $null }
        imgtool = if ($output -match '(?m)^2\.2\.0\s*$') { '2.2.0' } else { $null }
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
    $mcumgrPath = Resolve-ToolPath `
        -InjectedPath $env:STM32_FW_MCUMGR `
        -CommandName 'mcumgr.exe' `
        -KnownPaths @('D:\mcutools\bin\mcumgr.exe')
    $ncsRoot = if ($env:STM32_FW_NCS_ROOT) {
        $env:STM32_FW_NCS_ROOT
    } else {
        'D:\ncs\v3.4.0'
    }
    $halStm32Path = if ($env:HAL_STM32_PATH) {
        $env:HAL_STM32_PATH
    } else {
        'D:\ncs\extra\hal_stm32'
    }

    $nrfutil = New-ToolStatus $nrfutilPath
    $jlink = New-ToolStatus $jlinkPath
    $mcumgr = New-ToolStatus $mcumgrPath
    $ncs = New-DirectoryStatus $ncsRoot @('.west', 'zephyr')
    $toolchain = New-DirectoryStatus (Find-ToolchainRoot) @('manifest.json')
    $halStm32 = New-DirectoryStatus $halStm32Path @('zephyr')
    $toolVersions = if ($env:STM32_FW_SKIP_TOOL_EXEC) {
        [ordered]@{ available = $true }
    } elseif ($nrfutil.available -and $ncs.available) {
        Invoke-NcsVersionProbe $nrfutil.path $ncs.path
    } else {
        [ordered]@{ available = $false }
    }
    if ($mcumgr.available -and -not $env:STM32_FW_SKIP_TOOL_EXEC) {
        $mcumgr.version = (& $mcumgr.path version 2>&1 | Out-String).Trim()
    }
    $report = [ordered]@{
        ready = [bool](
            $nrfutil.available -and $jlink.available -and $mcumgr.available -and
            $ncs.available -and $toolchain.available -and $halStm32.available -and
            $toolVersions.available
        )
        requiredNcsVersion = $RequiredNcsVersion
        nrfutil = $nrfutil
        jlink = $jlink
        mcumgr = $mcumgr
        ncs = $ncs
        toolchain = $toolchain
        halStm32 = $halStm32
        tools = $toolVersions
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

function Invoke-Build {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') {
        throw 'Version must use MAJOR.MINOR.PATCH numeric syntax.'
    }
    $parsedVersion = [Version]$Version
    if ($parsedVersion -le [Version]'19.1.0') {
        throw 'Version must be greater than 19.1.0.'
    }

    $projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $resolvedBuildDir = [IO.Path]::GetFullPath($(if ($BuildDir) {
        $BuildDir
    } else {
        "D:\ncs-builds\stm32h563-$Version"
    }))
    $resolvedKeyPath = [IO.Path]::GetFullPath($(if ($KeyPath) {
        $KeyPath
    } else {
        Join-Path $projectRoot 'keys\stm32-mcuboot-ecdsa-p256.pem'
    }))
    $stageId = if ($DryRun) { 'dryrun' } else { [string]$PID }
    $stageParent = Split-Path -Parent $resolvedBuildDir
    $stageName = "$(Split-Path -Leaf $resolvedBuildDir)-source-stage-$stageId"
    $stageRoot = Join-Path $stageParent $stageName
    $stagedProject = Join-Path $stageRoot 'stm32'
    $stagedProtocol = Join-Path $stageRoot 'protocol'
    $nrfutilPath = Resolve-ToolPath `
        -InjectedPath $env:STM32_FW_NRFUTIL `
        -CommandName 'nrfutil' `
        -KnownPaths @('D:\nrfutil\nrfutil.exe')
    $ncsRoot = [IO.Path]::GetFullPath($(if ($env:STM32_FW_NCS_ROOT) {
        $env:STM32_FW_NCS_ROOT
    } else {
        'D:\ncs\v3.4.0'
    }))
    $halStm32Path = [IO.Path]::GetFullPath($(if ($env:HAL_STM32_PATH) {
        $env:HAL_STM32_PATH
    } else {
        'D:\ncs\extra\hal_stm32'
    }))
    $westArguments = @(
        'build', '-p', 'always', '--sysbuild',
        '-b', 'bciband_h563vg',
        '-d', $resolvedBuildDir,
        $stagedProject,
        '--',
        "-DBOARD_ROOT=$stagedProject",
        "-DZEPHYR_EXTRA_MODULES=$halStm32Path"
    )
    $report = [ordered]@{
        version = $Version
        projectRoot = $projectRoot
        buildDir = $resolvedBuildDir
        keyPath = $resolvedKeyPath
        stagedProject = $stagedProject
        westArguments = $westArguments
    }

    if ($DryRun) {
        if ($Json) { $report | ConvertTo-Json -Depth 4 } else { $report | Format-List }
        return
    }
    if (-not $nrfutilPath) { throw 'nrfutil was not found. Run doctor first.' }
    if (-not (Test-Path -LiteralPath $resolvedKeyPath -PathType Leaf)) {
        throw "Signing key not found: $resolvedKeyPath"
    }
    New-Item -ItemType Directory -Force -Path $resolvedBuildDir | Out-Null
    New-Item -ItemType Directory -Force -Path $stagedProject, $stagedProtocol | Out-Null
    Get-ChildItem -LiteralPath $projectRoot | Where-Object Name -ne 'keys' | Copy-Item `
        -Destination $stagedProject -Recurse -Force
    $protocolRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '..\protocol'))
    Get-ChildItem -LiteralPath $protocolRoot | Copy-Item `
        -Destination $stagedProtocol -Recurse -Force
    $stagedKeys = Join-Path $stagedProject 'keys'
    $stagedKey = Join-Path $stagedKeys 'stm32-mcuboot-ecdsa-p256.pem'
    New-Item -ItemType Directory -Force -Path $stagedKeys | Out-Null
    Copy-Item -LiteralPath $resolvedKeyPath -Destination $stagedKey -Force
    $stagedPrjConf = Join-Path $stagedProject 'prj.conf'
    $prjContent = Get-Content -Raw -LiteralPath $stagedPrjConf
    $versionPattern = '(?m)^CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="[^"]*"\r?$'
    if ($prjContent -notmatch $versionPattern) {
        throw 'Could not locate the MCUboot image version in staged prj.conf.'
    }
    $prjContent = [regex]::Replace(
        $prjContent,
        $versionPattern,
        "CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION=`"$Version`""
    )
    Set-Content -LiteralPath $stagedPrjConf -Encoding ascii -NoNewline -Value $prjContent
    $installRoot = Split-Path -Parent $ncsRoot
    $launchArguments = @(
        'sdk-manager', 'toolchain', 'launch',
        '--ncs-version', $RequiredNcsVersion,
        '--install-dir', $installRoot,
        '--chdir', $ncsRoot,
        '--', 'west'
    ) + $westArguments
    try {
        & $nrfutilPath @launchArguments
        $buildExitCode = $LASTEXITCODE
    } finally {
        if (Test-Path -LiteralPath $stagedKey -PathType Leaf) {
            Remove-Item -LiteralPath $stagedKey -Force
        }
    }
    if ($buildExitCode -ne 0) { exit $buildExitCode }

	$factoryHex = Join-Path $resolvedBuildDir "stm32h563_v$($Version.Replace('.', '_'))_factory.hex"
	$mergeScript = Join-Path $ncsRoot 'zephyr\scripts\build\mergehex.py'
	$mergeArguments = @(
		'sdk-manager', 'toolchain', 'launch',
		'--ncs-version', $RequiredNcsVersion,
		'--install-dir', $installRoot,
		'--chdir', $ncsRoot,
		'--', 'python', $mergeScript,
		'-o', $factoryHex,
		(Join-Path $resolvedBuildDir 'mcuboot\zephyr\zephyr.hex'),
		(Join-Path $resolvedBuildDir 'stm32\zephyr\zephyr.signed.hex')
	)
	& $nrfutilPath @mergeArguments
	if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $artifactPaths = [ordered]@{
		factoryHex = $factoryHex
        bootloaderHex = Join-Path $resolvedBuildDir 'mcuboot\zephyr\zephyr.hex'
        signedUpdateBin = Join-Path $resolvedBuildDir 'stm32\zephyr\zephyr.signed.bin'
        signedApplicationHex = Join-Path $resolvedBuildDir 'stm32\zephyr\zephyr.signed.hex'
        dfuZip = Join-Path $resolvedBuildDir 'dfu_application.zip'
    }
    $artifacts = [ordered]@{}
    foreach ($entry in $artifactPaths.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $entry.Value -PathType Leaf)) {
            throw "Expected build artifact is missing: $($entry.Value)"
        }
        $file = Get-Item -LiteralPath $entry.Value
        $artifacts[$entry.Key] = [ordered]@{
            path = $file.FullName
            bytes = $file.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
        }
    }
    $metadata = [ordered]@{
        version = $Version
        ncsVersion = $RequiredNcsVersion
        board = 'bciband_h563vg'
        buildDir = $resolvedBuildDir
        artifacts = $artifacts
    }
    $metadataPath = Join-Path $resolvedBuildDir 'build-metadata.json'
    $metadata | ConvertTo-Json -Depth 6 | Set-Content `
        -LiteralPath $metadataPath -Encoding utf8
    Write-Output "Build metadata: $metadataPath"
}

switch ($Command) {
    'doctor' { Invoke-Doctor }
    'build' { Invoke-Build }
    default {
        Write-Output 'Usage: pwsh -File tools/fw.ps1 <doctor|build> [options]'
    }
}
