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
#   -EnableOverclock       OC-priority: leave Intel's service ENABLED (see below)
#
# FAN vs OVERCLOCK - a hardware tradeoff you must pick:
#   The Intel Graphics Software service is REQUIRED for overclocking (it is the
#   precondition for ctlOverclockWaiverSet; without it every OC write returns
#   UNSUPPORTED_FEATURE 0x4000000a). BUT that same service also actively owns the
#   GPU fan, and it CONTENDS our fan service - with both running they fight over
#   fan ownership (canControl flips to no, our curve gets reverted to Intel stock).
#
#   DEFAULT = FAN-PRIORITY: this installer DISABLES the Intel service so our fan
#   curve applies reliably at boot. Overclocking is then unavailable until you run
#   an "OC session" (windows\oc-session.ps1) which briefly re-enables the service,
#   applies the OC, and it persists in hardware until the next reboot.
#
#   -EnableOverclock = OC-PRIORITY: leaves the Intel service enabled so OC works
#   and persists, but our custom fan curve will NOT hold (Intel manages the fan).
[CmdletBinding()]
param(
    [string]$BuildDir = (Join-Path $PSScriptRoot 'build\Release'),
    [switch]$NoService,
    [switch]$AddToPath,
    [switch]$EnableOverclock
)

# Intel's service owns the fan AND gates overclocking. Disabled by default so our
# fan curve wins; -EnableOverclock keeps it for OC at the cost of fan ownership.
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

if (-not $EnableOverclock) {
    # FAN-PRIORITY (default): disable the Intel service so it stops contending our
    # fan curve. This also disables overclocking until an OC session re-enables it
    # (windows\oc-session.ps1). Our boot service then owns the fan cleanly.
    foreach ($svc in $IntelOwnerServices) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s) { continue }
        try {
            if ($s.Status -ne 'Stopped') { Stop-Service -Name $svc -Force -ErrorAction Stop }
            Set-Service -Name $svc -StartupType Disabled -ErrorAction Stop
            Write-Host "Disabled '$($s.DisplayName)' ($svc) - fan-priority; run oc-session.ps1 to overclock."
        } catch {
            Write-Warning "Could not disable ${svc}: $_"
        }
    }
} else {
    # OC-PRIORITY: leave the Intel service enabled (OC works + persists), at the
    # cost of a reliable custom fan curve (Intel manages the fan while it runs).
    foreach ($svc in $IntelOwnerServices) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s) { continue }
        try {
            $startType = if ($svc -eq 'IntelGraphicsSoftwareService') { 'Automatic' } else { 'Manual' }
            Set-Service -Name $svc -StartupType $startType -ErrorAction Stop
            if ($s.Status -ne 'Running') { Start-Service -Name $svc -ErrorAction SilentlyContinue }
            Write-Host "Left '$($s.DisplayName)' ($svc) enabled - OC available; custom fan curve may not hold."
        } catch {
            Write-Warning "Could not enable ${svc}: $_"
        }
    }
    Write-Warning 'OC-priority mode: the Intel service will contend the fan; our custom curve may be reverted to Intel stock.'
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
