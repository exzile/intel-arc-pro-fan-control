# uninstall.ps1 — remove the Arc Fan Control tools + service.
#
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\uninstall.ps1
#
# By default the saved profile in %ProgramData%\ArcFanControl is kept; pass
# -PurgeConfig to remove it too.
[CmdletBinding()]
param(
    [switch]$PurgeConfig
)

$ErrorActionPreference = 'Stop'

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must be run from an elevated (Administrator) PowerShell.'
    }
}

Assert-Admin

$InstallDir = Join-Path $env:ProgramFiles 'ArcFanControl'
$svcExe = Join-Path $InstallDir 'arc-fan-service.exe'

if (Test-Path $svcExe) {
    Write-Host 'Stopping + removing the ArcFanControl service ...'
    & $svcExe uninstall
} else {
    # Fall back to sc.exe in case the binary is already gone.
    sc.exe stop  ArcFanControl 2>$null | Out-Null
    sc.exe delete ArcFanControl 2>$null | Out-Null
}

$lnk = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Arc GPU Dashboard.lnk'
if (Test-Path $lnk) { Remove-Item -Force $lnk }

if (Test-Path $InstallDir) {
    Write-Host "Removing $InstallDir ..."
    Remove-Item -Recurse -Force $InstallDir
}

# Remove from system PATH if present.
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($machinePath -like "*$InstallDir*") {
    $new = ($machinePath -split ';' | Where-Object { $_ -and $_ -ne $InstallDir }) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $new, 'Machine')
}

$DataDir = Join-Path $env:ProgramData 'ArcFanControl'
if ($PurgeConfig -and (Test-Path $DataDir)) {
    Write-Host "Removing config $DataDir ..."
    Remove-Item -Recurse -Force $DataDir
} elseif (Test-Path $DataDir) {
    Write-Host "Kept config at $DataDir (pass -PurgeConfig to remove)."
}

Write-Host 'Uninstalled.'
