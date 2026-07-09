# Windows installer (Inno Setup)

Builds a single **`ArcGpuControl-Setup.exe`** that installs Arc GPU Control for
end users — no cloning or compiling required.

## What the installer does

- Copies `arc-gpu.exe`, `arc-fan-service.exe`, `arc-gpu-gui.exe` to
  `C:\Program Files\ArcFanControl` (requires Administrator).
- Runs `install.ps1`, which:
  - registers + starts the **ArcFanControl** boot service (re-applies the saved
    fan curve + overclock at every boot),
  - **disables** the Intel Graphics Software service (it contends the fan; our
    service then owns both fan and OC — see the main `README.md`),
  - grants standard users write access to the `%ProgramData%\ArcFanControl`
    profile so the non-elevated GUI can save changes,
  - adds a Start-Menu shortcut and a login auto-start for the tray icon.
- Offers to launch the tray app when finished.

Uninstalling (Add/Remove Programs) runs `uninstall.ps1 -KeepInstallDir`, which
stops + removes the service, re-enables the Intel service, and clears the
auto-start; Inno then removes the program files. The saved profile in
`%ProgramData%\ArcFanControl` is kept unless you remove it manually.

## Building the installer

```powershell
# From windows\installer\ — builds Release binaries, then compiles the setup:
powershell -ExecutionPolicy Bypass -File build-installer.ps1
```

Requirements: Visual Studio + CMake (to build the binaries) and
[Inno Setup 6](https://jrsoftware.org/isdl.php) (`ISCC.exe`). Pass `-SkipBuild`
if the binaries are already built, or `-Iscc <path>` for a non-default ISCC.

Output: `installer\output\ArcGpuControl-Setup.exe`. Attach that file to a GitHub
Release so users can download and run it. (The output is git-ignored — it's a
build artifact, produced by the command above, not committed.)

## Notes

- **B70 overclocking is board/firmware-specific** — the installer still works
  regardless. The B60 always accepts overclock writes. B70 support is
  auto-detected per card at runtime: an Intel-reference B70 (`8086:1701`) is
  firmware-locked and stays gated (tested), while an ASRock Arc Pro B70 Creator
  (subsystem `1849:6020`) is expected to enable OC automatically since its VF
  table is provisioned (not itself confirmed on ASRock hardware). Fan control
  works on every card either way.
- The app calls Intel's public IGCL (`ControlLib.dll`, a system driver DLL); no
  kernel driver is installed and nothing needs signing.
