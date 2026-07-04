## Confirmed: Intel patch series 168027 gives working fan control on Arc Pro B60 (8086:e211) — no MEI firmware needed

Following up my earlier comments on this issue. I applied Intel's fan-control patch series **168027** (Karthik Poosa, June 2026 — the `drm/xe/hwmon` fan-control series) and can confirm it gives **full working manual fan control on the Arc Pro B60**, a Pro Battlemage device ID that this issue was opened about.

### Hardware / environment
- **Card:** Intel Arc Pro B60 — Battlemage G21 — `8086:e211`, subsystem ASRock `1849:6023`
- **OS/Kernel:** Ubuntu 26.04 LTS, `7.0.0-27-generic`, `xe` driver
- **Patch:** series 168027 applied to the stock `xe_hwmon.c` + `xe_pcode_api.h` (applied with `--fuzz`, built as an out-of-tree `xe.ko`)

### What works
The patch exposes `fan1_max`, `pwm1`, `pwm1_enable`, and `pwm1_auto_point[1-10]_temp/_pwm`, all functional on the B60:

**Read path** — the driver reads the B60's real stock fan table from the `FAN_SPEED_CONTROL` PCODE mailbox:
```
fan1_max = 4980 RPM
stock curve (pwm1_auto_pointN): 59C->0  60C->51  65C->77  70C->102  75C->153  79C->204  84C->255 ...
```

**Write path** — all three `pwm1_enable` modes drive the fan correctly:
- `pwm1_enable=0` (full speed) → fan ramps `0 -> 976 -> 3799 -> 4852 -> 4940 RPM` (≈ fan1_max).
- `pwm1_enable=1` + a custom 10-point `pwm1_auto_point*` curve → fan holds a curve-appropriate ~2400 RPM at 48 °C and actively cools the GPU (48 °C -> 44 °C).
- Direct `pwm1` writes also work: `pwm1=180` → ~3540 RPM, `pwm1=60` → ~1171 RPM.
- No `dmesg` faults throughout.

### The key finding for this issue
**The B60 did NOT need the missing MEI late-bind firmware blob (`fan_control_8086_e211.bin`) for manual control.** That firmware is only the *autonomous stock* fan response; the **user fan table is programmed purely over the `FAN_SPEED_CONTROL` PCODE mailbox**, and the B60's PCODE accepts the `FSC_WRITE_FAN_TABLE` / `FSC_WRITE_NUM_FAN_CONTROL_POINTS` subcommands directly. (Confirmed separately: in auto-stock mode the fan does *not* spin autonomously — 0 RPM at 60 °C — consistent with the missing autonomous firmware, but that does not block the manual PCODE path.)

So for Pro Battlemage cards, **series 168027 alone is sufficient for manual/curve fan control** on Linux — no new firmware required. It would be great to see `has_fan_control` confirmed/enabled for the Pro device IDs (`e211`, `e223`) as this series lands. Happy to test further on both B60 and B70.
