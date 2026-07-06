# Windows port — plan, capability map & status

This tracks the port of the Linux Arc Pro toolkit to Windows. The Linux side
drives the GPU's **PCODE mailbox** directly from a patched `xe.ko`; on Windows
the vendor driver owns that mailbox, so we go through Intel's **Graphics Control
Library (IGCL / ControlLib.dll)**, which surfaces the same capabilities as a
supported userspace API — no kernel driver, nothing to sign.

## Decision: IGCL, not a mailbox driver

The alternatives considered and why IGCL won:

| Approach | Verdict |
|---|---|
| **IGCL library** (chosen) | Native fan tables, VF-curve read/write, power/temp limits, and rich telemetry. No driver signing. Same path Intel's own Arc Control uses → most likely to also cover B70/G31. |
| Custom KMD driver mirroring the Linux mailbox pokes | Full control, but must be signed; duplicates what IGCL already exposes; highest brick risk. |
| WinRing0-style MMIO shim from userspace | Fast to prototype but fights the vendor driver for the mailbox; signing/security caveats; fragile. |

## Capability map: Linux → Windows (IGCL)

| Capability | Linux (this repo) | Windows (IGCL) | Ported |
|---|---|---|---|
| Fan curve | `pwm1_auto_point*` (patch 168027) | `ctlFanSetSpeedTableMode` | ✅ |
| Fan full speed | `pwm1_enable=0` | `ctlFanSetFixedSpeedMode` 100% | ✅ |
| Fan auto/stock | `pwm1_enable=2` | Intel stock *curve* via `ctlFanSetSpeedTableMode` | ✅ (NOT `ctlFanSetDefaultMode` — see below) |
| Fan RPM / % read | `fan1_input` / `pwm1` | `ctlFanGetState` | ✅ |
| VF curve read/write | `xe_gt_oc` `0x5f/0x5d` transaction | `ctlOverclockRead/WriteCustomVFCurve` | ✅ |
| GPU freq offset | (n/a — Linux used min/max clamps) | `ctlOverclockGpuFrequencyOffsetSetV2` | ✅ |
| GPU voltage offset | VF-curve shift | `ctlOverclockGpuMaxVoltageOffsetSetV2` | ✅ |
| VRAM memory speed | `oc/mem_speed` (`0x5e/0x17`) | `ctlOverclockVramMemSpeedLimitSetV2` | ✅ |
| Temperature limit | `oc/temp_limit` (`0x5e/0x49`) | `ctlOverclockTemperatureLimitSetV2` | ✅ |
| Power (TDP) limit | `power1_cap` | `ctlOverclockPowerLimitSetV2` | ✅ |
| Reset OC to stock | `xe-gpu-oc reset` | `ctlOverclockResetToDefault` | ✅ |
| Power draw | derived from `energy*_input` | `ctlPowerTelemetryGet` energy counters | ✅ |
| GPU/render/media utilisation % | **not available on Linux xe** | telemetry activity counters | ✅ (bonus) |
| VRAM bandwidth | **not available on Linux xe** | telemetry bandwidth counters | ✅ (bonus) |
| Throttle reasons | `freq0/throttle/reason_*` | telemetry `gpu*Limited` flags | ✅ |
| Boot persistence | systemd `*.service` | `arc-fan-service` (SCM) | ✅ |
| Multi-GPU targeting | `ARC_GPU_BDF` | `--bdf` / `ARC_GPU_BDF` | ✅ |
| Native GUI | GTK4 `xe-gpu-gui` | Win32/GDI `arc-gpu-gui` | 🟡 dashboard + draggable fan-curve editor done; OC tab pending |
| Per-sensor temp table | `xe-gpu-temps` (12 VRAM channels) | `ctlEnumTemperatureSensors` → `arc-gpu temps` | ✅ (IGCL exposes GPU/VRAM/global, not 12 VRAM channels) |
| Named OC profiles | `xe-gpu-oc profile save/load` | `arc-gpu oc profile save/load/list/delete` | ✅ |
| Stability test | `xe-gpu-stress` | — | ⬜ not yet |
| VRAM used/total | root debugfs → `xe-gpu-vramd` | `ctlMemoryGetState` → `arc-gpu status` | ✅ (no root needed) |

## Notable differences from Linux

- **Fan unit is percent (0-100)**, not PWM (0-255). `fan_curve.hpp::pwmToPercent`
  converts old curves.
- **GPU clock tuning is offset-based** (IGCL exposes a frequency *offset*, not
  the min/max clamp the Linux `xe-gpu-tune --clk-min/--clk-max` used). Power and
  temperature limits map directly.
- **The B70/G31 OC gap is real on Windows too — confirmed.** The G31 firmware
  gates overclocking at the PCODE level regardless of OS, and Intel's own Windows
  Arc Control app exposes **no tuning section** for the B70. Only the **B60/G21**
  is overclockable here. Fan control works on both.

## OC ownership & the warranty waiver (Windows-specific)

Overclocking on Windows has two gotchas the Linux side never had:

- **The waiver is the real gate — and it PERSISTS per boot.** All `ctlOverclock*`
  setters require `ctlOverclockWaiverSet` to have succeeded first, else they fail
  with `DATA_WRITE`. The waiver only succeeds while the **Intel Graphics Software
  service** is running (as admin). But once granted, the driver keeps the waiver
  until the next reboot — so you can grant it via a brief Intel-service window,
  disable the service, and OC still applies. `install.ps1` is fan-priority by
  default; `windows\oc-session.ps1` opens that brief window on demand.
- **The Intel service contends the fan while running**, so we grant-then-disable
  rather than leave it up. From a clean boot our fan curve owns the fan whenever
  the service is off.
- **`ctlFanSetDefaultMode` is banned.** It permanently relinquishes fan ownership
  for the driver session; the public IGCL fan API then returns SUCCESS but
  silently no-ops on every table write until a driver reset (reboot or
  `pnputil /restart-device`). `fanSetAuto` applies Intel's stock *curve* via table
  mode instead, so we never lose ownership.
- **Under the hood** both fan and OC ride the same private DXGK escape `0x80c`
  (Type=0 driver-private, dispatched by `IntelControlLib.dll`); the kernel does
  not gate it, only the userspace waiver + persisted-waiver state do. The full
  wire format and the OC param map (`0x2f` freq, `0x36` temp, `0x30` power, `0x32`
  volt, `0x33` mem, `0x25` reset, `0x29` waiver) were reverse-engineered but are
  **not needed** given the persisted-waiver approach above.

## Roadmap (next)

1. ~~Per-sensor temperature table~~ — **done** (`arc-gpu temps`).
2. ~~Named OC profiles~~ — **done** (`arc-gpu oc profile save/load/list/delete`).
3. ~~VRAM used/total + memory props~~ — **done** (`arc-gpu status`, via
   `ctlMemoryGetState`; no root needed unlike the Linux debugfs path).
4. **Native GUI** — 🟡 partial: the Win32/GDI **dashboard** and the **draggable
   fan-curve editor** (`arc-gpu-gui`) are done. Still to add: the VF-curve/OC
   editor tab from the Linux GUI (use `arc-gpu oc` meanwhile).
5. **Stability test** — a fan-guarded GPU load with auto-revert, porting
   `xe-gpu-stress`.
6. **Installer** — 🟡 partial: a PowerShell `install.ps1`/`uninstall.ps1` copies
   the binaries, creates the ProgramData dir, registers/starts the service, and
   adds a Start-Menu shortcut. A signed MSI/WiX package is still a nice-to-have.
7. **Boot-time OC waiver orchestration** — ⬜ planned: have `arc-fan-service`
   briefly enable the Intel service at startup to grant the OC waiver, apply the
   saved overclock, then disable it and apply the fan curve — so fan + OC are both
   live after every boot with no manual `oc-session.ps1`. (`oc-session.ps1` covers
   the manual path today.)
8. **System-tray GUI** — ✅ `arc-gpu-gui --tray`: notification-area icon that
   opens the dashboard (left/double-click) with an Auto/Max/Exit menu
   (right-click); auto-starts at login.

## Testability note

Everything under `windows/` targets the Win32 + IGCL runtime and cannot be built
or exercised on the Linux CI container this port was authored in. It compiles
against `igcl_api.h` and links `ControlLib.dll` at runtime; validation requires a
Windows box with an Intel Arc driver. The code is structured so `arc_core` is a
plain library that a future unit test / GUI can link without the CLI or service.
