# oc-session.ps1 - apply an overclock on a fan-priority install.
#
# The default install is FAN-PRIORITY: the Intel Graphics Software service is
# disabled so our fan curve owns the fan. That service is also the precondition
# for the overclock waiver, so overclocking is unavailable while it is disabled.
#
# This script opens a short "OC session": it re-enables + starts the Intel
# service, applies your overclock via arc-gpu, and then (unless -KeepIntelService)
# disables the service again. Overclock values are written to the GPU and PERSIST
# in hardware until the next reboot / driver reset - you do NOT need the Intel
# service running to keep them, only to (re)apply them.
#
# Run from an elevated PowerShell. Examples:
#   powershell -ExecutionPolicy Bypass -File windows\oc-session.ps1 -Oc 'freq 100','temp 95'
#   powershell -ExecutionPolicy Bypass -File windows\oc-session.ps1 -Status
#   powershell -ExecutionPolicy Bypass -File windows\oc-session.ps1 -Oc 'reset'
[CmdletBinding()]
param(
    # One or more arc-gpu 'oc' argument strings, e.g. 'freq 100', 'volt 20',
    # 'temp 95', 'mem 19', 'reset'. Applied in order.
    [string[]]$Oc,
    # Just print current OC / tune state and exit (still needs the service briefly).
    [switch]$Status,
    # Leave the Intel service running after the session (fan curve will not hold).
    [switch]$KeepIntelService
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
$exe = Join-Path $InstallDir 'arc-gpu.exe'
if (-not (Test-Path $exe)) { throw "arc-gpu.exe not found at $exe - install first." }

$IntelSvc  = 'IntelGraphicsSoftwareService'
$IntelDisc = 'IGSDSserviceDiscrete'

Write-Host 'Opening OC session: enabling the Intel service (overclock waiver precondition) ...'
Set-Service -Name $IntelSvc -StartupType Automatic
Start-Service -Name $IntelSvc -ErrorAction SilentlyContinue
Start-Service -Name $IntelDisc -ErrorAction SilentlyContinue
Start-Sleep -Seconds 6

try {
    if ($Status -or -not $Oc) {
        Write-Host '--- current overclock / tune state ---'
        & $exe tune show
    }
    if ($Oc) {
        foreach ($cmd in $Oc) {
            $parts = $cmd.Trim() -split '\s+'
            Write-Host "Applying: oc $cmd"
            & $exe oc @parts
            if ($LASTEXITCODE -ne 0) { Write-Warning "  'oc $cmd' returned exit code $LASTEXITCODE" }
        }
        Write-Host '--- overclock state after apply ---'
        & $exe tune show
    }
}
finally {
    if ($KeepIntelService) {
        Write-Host 'Leaving the Intel service RUNNING (-KeepIntelService): your custom fan curve may not hold.'
    } else {
        Write-Host 'Closing OC session: disabling the Intel service so the fan curve owns the fan again ...'
        if ((Get-Service $IntelDisc -ErrorAction SilentlyContinue).Status -eq 'Running') {
            Stop-Service -Name $IntelDisc -Force -ErrorAction SilentlyContinue
        }
        Set-Service -Name $IntelDisc -StartupType Disabled -ErrorAction SilentlyContinue
        if ((Get-Service $IntelSvc).Status -eq 'Running') { Stop-Service -Name $IntelSvc -Force }
        Set-Service -Name $IntelSvc -StartupType Disabled
        Write-Host 'Intel service disabled. Overclock values remain applied until the next reboot.'
        Write-Host 'Reboot to have the fan service re-take the fan cleanly (OC will need another session).'
    }
}
