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
| Fan auto/stock | `pwm1_enable=2` | `ctlFanSetDefaultMode` | ✅ |
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
| Native GUI | GTK4 `xe-gpu-gui` | — | ⬜ not yet |
| Per-sensor temp table | `xe-gpu-temps` (12 VRAM channels) | `ctlEnumTemperatureSensors` → `arc-gpu temps` | ✅ (IGCL exposes GPU/VRAM/global, not 12 VRAM channels) |
| Named OC profiles | `xe-gpu-oc profile save/load` | `arc-gpu oc profile save/load/list/delete` | ✅ |
| Stability test | `xe-gpu-stress` | — | ⬜ not yet |
| VRAM used/total | root debugfs → `xe-gpu-vramd` | IGCL memory props | ⬜ not yet |

## Notable differences from Linux

- **Fan unit is percent (0-100)**, not PWM (0-255). `fan_curve.hpp::pwmToPercent`
  converts old curves.
- **GPU clock tuning is offset-based** (IGCL exposes a frequency *offset*, not
  the min/max clamp the Linux `xe-gpu-tune --clk-min/--clk-max` used). Power and
  temperature limits map directly.
- **The B70/G31 gap may not exist here.** On Linux the G31 rejected the private
  PCODE OC opcodes (`-EPROTO`); IGCL routes through the vendor driver, so if the
  Windows Arc Control app can tune the card, these same calls should too. To be
  confirmed on hardware — `ctlOverclockGetProperties().bSupported` reports it.

## Roadmap (next)

1. ~~Per-sensor temperature table~~ — **done** (`arc-gpu temps`).
2. ~~Named OC profiles~~ — **done** (`arc-gpu oc profile save/load/list/delete`).
3. **VRAM used/total + memory props** — surface from IGCL for the dashboard.
4. **Native GUI** — a WinUI 3 / Win32 dashboard + fan-curve + OC editor over
   `arc_core` (the CLI and service already share that library).
5. **Stability test** — a fan-guarded GPU load with auto-revert, porting
   `xe-gpu-stress`.
6. **Installer** — MSI/WiX that fetches nothing, drops the binaries, creates the
   ProgramData profile with a sane ACL, and registers the service.

## Testability note

Everything under `windows/` targets the Win32 + IGCL runtime and cannot be built
or exercised on the Linux CI container this port was authored in. It compiles
against `igcl_api.h` and links `ControlLib.dll` at runtime; validation requires a
Windows box with an Intel Arc driver. The code is structured so `arc_core` is a
plain library that a future unit test / GUI can link without the CLI or service.
