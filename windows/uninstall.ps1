# uninstall.ps1 - remove the Arc Fan Control tools + service.
#
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\uninstall.ps1
#
# By default the saved profile in %ProgramData%\ArcFanControl is kept; pass
# -PurgeConfig to remove it too.
[CmdletBinding()]
param(
    [switch]$PurgeConfig,
    # Set by the Inno Setup uninstaller: it removes the program files + PATH itself,
    # so this script only stops/removes the service, re-enables Intel, and clears
    # the tray auto-start.
    [switch]$KeepInstallDir
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

# Remove the login auto-start entry for the tray icon.
Remove-ItemProperty -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ArcGpuControl' -ErrorAction SilentlyContinue

if (-not $KeepInstallDir) {
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
}

# Restore the Intel Graphics Software services to enabled, in case this machine
# was installed with -DisableIntelService (fan-only). Harmless no-op otherwise.
$IntelOwnerServices = @('IntelGraphicsSoftwareService', 'IGSDSserviceDiscrete')
foreach ($svc in $IntelOwnerServices) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) { continue }
    try {
        Set-Service -Name $svc -StartupType Automatic -ErrorAction Stop
        Start-Service -Name $svc -ErrorAction SilentlyContinue
        Write-Host "Re-enabled '$($s.DisplayName)' ($svc)."
    } catch {
        Write-Warning "Could not re-enable ${svc}: $_"
    }
}

$DataDir = Join-Path $env:ProgramData 'ArcFanControl'
if ($PurgeConfig -and (Test-Path $DataDir)) {
    Write-Host "Removing config $DataDir ..."
    Remove-Item -Recurse -Force $DataDir
} elseif (Test-Path $DataDir) {
    Write-Host "Kept config at $DataDir (pass -PurgeConfig to remove)."
}

Write-Host 'Uninstalled.'
