# build-installer.ps1 — build the binaries (Release) and compile the Inno Setup
# installer into installer\output\ArcGpuControl-Setup.exe.
#
#   powershell -ExecutionPolicy Bypass -File build-installer.ps1
#
# Requires: Visual Studio + CMake, and Inno Setup 6 (ISCC.exe). Pass -Iscc to
# point at a non-default ISCC location.
[CmdletBinding()]
param(
    [string]$Iscc,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'

$installerDir = $PSScriptRoot
$windowsDir   = Split-Path -Parent $installerDir   # ...\windows

if (-not $SkipBuild) {
    Write-Host 'Building Release binaries...' -ForegroundColor Cyan
    Push-Location $windowsDir
    try {
        if (-not (Test-Path (Join-Path $windowsDir 'build'))) {
            & cmake -B build -A x64
            if ($LASTEXITCODE -ne 0) { throw "cmake configure failed ($LASTEXITCODE)" }
        }
        & cmake --build build --config Release
        if ($LASTEXITCODE -ne 0) { throw "cmake build failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}

foreach ($b in 'arc-gpu.exe', 'arc-fan-service.exe', 'arc-gpu-gui.exe') {
    $p = Join-Path $windowsDir "build\Release\$b"
    if (-not (Test-Path $p)) { throw "Missing built binary: $p (build first)" }
}

# Locate ISCC.
if (-not $Iscc) {
    $Iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc -or -not (Test-Path $Iscc)) {
    throw "Inno Setup ISCC.exe not found. Install Inno Setup 6 (https://jrsoftware.org/isdl.php) or pass -Iscc <path>."
}

Write-Host "Compiling installer with $Iscc ..." -ForegroundColor Cyan
& $Iscc (Join-Path $installerDir 'arc-gpu-control.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

$out = Join-Path $installerDir 'output\ArcGpuControl-Setup.exe'
Write-Host ""
Write-Host "Installer built: $out" -ForegroundColor Green
