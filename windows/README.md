# Intel Arc Pro — Windows port (IGCL)

A Windows control layer for the Intel Arc **Pro B60 / B70** (Battlemage) that
mirrors this repo's Linux toolkit — **custom fan curves**, **power/temperature
limits**, **voltage-frequency-curve overclocking**, live **telemetry**, and a
boot **service** that re-applies your profile — built on Intel's own
**Graphics Control Library (IGCL / ControlLib.dll)** instead of the Linux
`xe` sysfs + PCODE patches.

> **Why IGCL and not a PCODE mailbox driver?** On Linux this project pokes the
> GPU's PCODE mailbox directly from a patched `xe.ko`. On Windows the vendor
> driver already owns that mailbox, and Intel exposes the same capabilities as a
> supported userspace API. IGCL gives us fan tables, VF-curve read/write, power
> and temperature limits, and rich telemetry with **no kernel driver to sign** —
> and it works through the same driver path the Windows Arc Control app uses, so
> it should cover the B70/G31 too (which rejected the Linux PCODE OC path).

## Status

This is the **first cut** of the port: the IGCL core, the `arc-gpu` CLI, and the
`arc-fan-service` boot service are implemented. A native GUI is not ported yet.
See [PORT.md](PORT.md) for the full capability map and roadmap.

## Layout

```
windows/
  CMakeLists.txt              build (MSVC / clang-cl, C++17)
  third_party/igcl/           drop igcl_api.h here (not vendored — see its README)
  src/
    igcl.hpp / igcl.cpp       dynamic ControlLib.dll loader
    arc.hpp  / arc.cpp        high-level Arc wrapper (fan / OC / telemetry)
    fan_curve.hpp / .cpp      "temp:percent" curve parsing
    config.hpp / .cpp         %ProgramData%\ArcFanControl\config.ini persistence
    apply.hpp / .cpp          apply a saved profile (shared by CLI + service)
    cli/main.cpp              arc-gpu command-line tool
    service/service_main.cpp  arc-fan-service Windows service
    gui/main.cpp              arc-gpu-gui Win32/GDI dashboard
```

## Build

Prerequisites: Windows 10/11, Visual Studio 2022 (or Build Tools) with the C++
workload, CMake ≥ 3.20, and a supported Intel Arc graphics driver installed.

```powershell
# 1. fetch the IGCL header (one time) — see third_party/igcl/README.md
curl -L -o windows/third_party/igcl/igcl_api.h `
  https://raw.githubusercontent.com/intel/drivers.gpu.control-library/master/include/igcl_api.h

# 2. configure + build
cd windows
cmake -B build -A x64
cmake --build build --config Release
# -> build/Release/arc-gpu.exe, arc-fan-service.exe, arc-gpu-gui.exe
```

## Desktop dashboard — `arc-gpu-gui`

A native Win32/GDI window (no external toolkit) with two views:

- **Dashboard** — a live 1 s view of clocks, card/GPU power, temperature,
  utilisation, fan and VRAM, with a GPU selector and **Fan Auto / Fan Max**.
- **Fan Curve** — a draggable temperature→percent editor: drag nodes,
  double-click to add a point, right-click a node to remove it, with a live
  GPU-temperature marker, then **Apply Curve** (persists + hands to the service)
  or **Reset**.

Run it elevated so fan writes take effect. The VF-curve/overclock editor tab
isn't ported yet — use `arc-gpu oc` for that.

## Use (run from an elevated / Administrator prompt)

```powershell
arc-gpu list                       # enumerate Intel adapters
arc-gpu status                     # clocks / power / temp / utilisation / fan

# fan (percent, 0-100 — not PWM bytes like the Linux CLI)
arc-gpu fan show
arc-gpu fan set 45:30 55:50 65:70 75:90 85:100
arc-gpu fan auto                   # hand back to the stock auto table
arc-gpu fan max                    # full speed
arc-gpu fan fixed 60               # hold 60%

# power / overclock (needs a supported card + driver)
arc-gpu tune power 150             # sustained power limit, Watts
arc-gpu oc read                    # dump the live VF curve
arc-gpu oc freq 100                # GPU frequency offset
arc-gpu oc volt 25                 # GPU voltage offset (accepts warranty waiver)
arc-gpu oc mem 20                  # VRAM memory-speed limit (IGCL units)
arc-gpu oc temp 95                 # GPU thermal-throttle target
arc-gpu oc vfcurve 820:1200 900:1800 1035:2400   # custom VF curve (waiver)
arc-gpu oc reset                   # back to stock
arc-gpu oc profile save daily      # save current OC as a named profile
arc-gpu oc profile load daily      # re-apply it (also: list / delete)

# temperatures (per sensor: GPU / VRAM / global)
arc-gpu temps

# multi-GPU: target a specific card by BDF (or set ARC_GPU_BDF)
arc-gpu --bdf 03:00.0 fan set 50:40 70:80 85:100
```

Every `fan set/auto/max/fixed`, `tune power`, and `oc freq/volt/mem/temp` also
**persists** the choice to `%ProgramData%\ArcFanControl\config.ini`.

## Boot persistence (service)

Arc resets to stock fan/overclock on cold boot, driver reset, and resume. The
service re-applies your saved profile at startup and every 60 s thereafter:

```powershell
arc-fan-service install     # register + auto-start (run elevated)
arc-fan-service uninstall    # stop + remove
arc-fan-service run          # foreground, for debugging
```

Its log is `%ProgramData%\ArcFanControl\service.log`.

## Notes & caveats

- **Units differ from Linux.** The Linux CLI uses PWM `0-255`; IGCL fan tables
  use **percent (0-100)**, so this port speaks percent. `fan_curve.hpp` has a
  `pwmToPercent()` helper if you're porting an old curve.
- **Overclock warranty waiver.** IGCL requires accepting a warranty waiver
  before voltage / VF-curve writes; the tool sets it for you on those commands.
  Overclocking can reduce the part's lifetime — use at your own risk.
- **Permissions.** IGCL writes and the ProgramData profile need Administrator.
- **Not affiliated with or endorsed by Intel.** IGCL is Intel's library; this
  port only calls its public API. Use at your own risk.
