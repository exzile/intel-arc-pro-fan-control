# install.ps1 - install the Arc Fan Control Windows tools + service.
#
# Windows analogue of the repo's install.sh. Copies the built binaries to
# Program Files, creates the ProgramData config directory, registers and starts
# the boot service, and adds a Start-Menu shortcut for the GUI.
#
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
#
# Options:
#   -BuildDir <path>       where the .exe files are (default: build\Release)
#   -NoService             copy binaries but don't register the service
#   -AddToPath             add the install dir to the system PATH
#   -DisableIntelService   fan-only: disable Intel's Graphics Software service
#
# The Intel Graphics Software service is REQUIRED for overclocking: it is the
# precondition for ctlOverclockWaiverSet (without it running, every OC write
# returns UNSUPPORTED_FEATURE 0x4000000a). It does NOT block our fan control -
# fan and OC both work while it runs (verified: fan reaches full RPM + OC freq
# offset applies with the service up). So by default we KEEP it enabled.
#
# Pass -DisableIntelService only if you want fan control with zero possibility of
# the Intel Arc app contending the fan curve, and you don't need overclocking.
# Doing so DISABLES overclocking (the waiver can no longer be set).
[CmdletBinding()]
param(
    [string]$BuildDir = (Join-Path $PSScriptRoot 'build\Release'),
    [switch]$NoService,
    [switch]$AddToPath,
    [switch]$DisableIntelService
)

# The Intel service is the overclock-waiver precondition; keep it running unless
# the user explicitly opts into fan-only (-DisableIntelService).
$IntelOwnerServices = @('IntelGraphicsSoftwareService', 'IGSDSserviceDiscrete')

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
$Binaries   = @('arc-gpu.exe', 'arc-fan-service.exe', 'arc-gpu-gui.exe')

# Verify the build output exists.
foreach ($b in $Binaries) {
    $src = Join-Path $BuildDir $b
    if (-not (Test-Path $src)) {
        throw "Missing '$src'. Build first: cmake -B build -A x64; cmake --build build --config Release"
    }
}

Write-Host "Installing to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($b in $Binaries) {
    Copy-Item -Force (Join-Path $BuildDir $b) (Join-Path $InstallDir $b)
}

# ProgramData config dir (the service + tools read %ProgramData%\ArcFanControl).
$DataDir = Join-Path $env:ProgramData 'ArcFanControl'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
# Grant standard users Modify so the non-elevated CLI/GUI can save profiles that
# the SYSTEM service reads (S-1-5-32-545 = BUILTIN\Users; /T covers existing files).
& icacls "$DataDir" /grant '*S-1-5-32-545:(OI)(CI)M' /T /C 2>&1 | Out-Null

if ($AddToPath) {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ($machinePath -notlike "*$InstallDir*") {
        [Environment]::SetEnvironmentVariable('Path', "$machinePath;$InstallDir", 'Machine')
        Write-Host "Added $InstallDir to the system PATH (restart shells to pick it up)."
    }
}

# Start-Menu shortcut for the GUI.
try {
    $startMenu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'
    $lnk = Join-Path $startMenu 'Arc GPU Dashboard.lnk'
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $InstallDir 'arc-gpu-gui.exe'
    $s.WorkingDirectory = $InstallDir
    $s.Description = 'Arc GPU Dashboard'
    $s.Save()
    Write-Host 'Created Start-Menu shortcut "Arc GPU Dashboard".'
} catch {
    Write-Warning "Could not create Start-Menu shortcut: $_"
}

# Auto-start the tray icon at login (all users) so it's always in the notification
# area. --tray launches straight to the tray (window hidden until clicked).
try {
    $runKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
    Set-ItemProperty -Path $runKey -Name 'ArcGpuControl' -Value "`"$InstallDir\arc-gpu-gui.exe`" --tray"
    Write-Host 'Registered the tray icon to auto-start at login.'
} catch {
    Write-Warning "Could not register tray auto-start: $_"
}

if (-not $DisableIntelService) {
    # Keep the Intel service ENABLED + running: it is the overclock-waiver
    # precondition (OC writes return UNSUPPORTED_FEATURE without it). Our fan
    # control coexists with it, so there is no reason to disable it by default.
    foreach ($svc in $IntelOwnerServices) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s) { continue }
        try {
            $startType = if ($svc -eq 'IntelGraphicsSoftwareService') { 'Automatic' } else { 'Manual' }
            Set-Service -Name $svc -StartupType $startType -ErrorAction Stop
            if ($s.Status -ne 'Running') { Start-Service -Name $svc -ErrorAction SilentlyContinue }
            Write-Host "Ensured '$($s.DisplayName)' ($svc) is enabled - required for overclocking."
        } catch {
            Write-Warning "Could not enable ${svc}: $_"
        }
    }
} else {
    # Fan-only: disable the Intel service. This DISABLES overclocking.
    foreach ($svc in $IntelOwnerServices) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s) { continue }
        try {
            if ($s.Status -ne 'Stopped') { Stop-Service -Name $svc -Force -ErrorAction Stop }
            Set-Service -Name $svc -StartupType Disabled -ErrorAction Stop
            Write-Host "Disabled '$($s.DisplayName)' ($svc) - fan-only mode, overclocking unavailable."
        } catch {
            Write-Warning "Could not disable ${svc}: $_"
        }
    }
    Write-Warning 'Overclocking is DISABLED (-DisableIntelService). Re-run install.ps1 without that switch to restore OC.'
}

if (-not $NoService) {
    Write-Host 'Registering + starting the ArcFanControl service ...'
    & (Join-Path $InstallDir 'arc-fan-service.exe') install
    if ($LASTEXITCODE -ne 0) { Write-Warning "Service registration returned exit code $LASTEXITCODE." }
} else {
    Write-Host 'Skipped service registration (-NoService).'
}

Write-Host ''
Write-Host 'Done. Try:  arc-gpu status'
Write-Host 'Set a fan curve:  arc-gpu fan set 45:30 55:50 65:70 75:90 85:100'
Write-Host 'The service re-applies your saved profile at boot.'
