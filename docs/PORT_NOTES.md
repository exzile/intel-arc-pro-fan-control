# Fan-control patch (Intel series 168027) — prep notes for the B60 on Ubuntu 7.0.0

## What's in this dir
- `xe-fan-control-168027-cachyos-7.1.2.patch` — the adapted patch (PerkyZZ999, from Intel series 168027). Touches 3 files: `xe_hwmon.c`, `xe_pcode_api.h`, and an ABI doc.
- `xe_hwmon.c.patched` — the COMPLETE patched `xe_hwmon.c` (CachyOS 7.1.2). Use as the authoritative reference for any hand-port / reject fix.
- `series-168027.mbox` (parent scratchpad) — the raw Intel 5-patch series.
- `apply_xefan.sh` — run ON THE UBUNTU BOX: restores pristine 7.0.0 source, dry-runs the patch, and (with APPLY=1) applies + builds the module.

## The mechanism (what the patch actually does)
Fan curve = PCODE `FAN_SPEED_CONTROL` (0x7d), param2 = fan number:
- per point: `xe_pcode_write(PCODE_MBOX(0x7d, FSC_WRITE_FAN_TABLE=0x1, fan), temp | speed<<8)`  (temp °C in bits[7:0], PWM 0-255 in bits[15:8])
- commit:   `xe_pcode_write(PCODE_MBOX(0x7d, FSC_WRITE_NUM_FAN_CONTROL_POINTS=0x0, fan), point_count)`
- modes via `pwm1_enable`: 0=full speed, 1=manual user table, 2=auto stock. Up to 10 points.
Exposes sysfs: `pwm1`, `pwm1_enable`, `fan1_max`, `pwm1_auto_point[1-10]_temp/_pwm`.

## Adaptation risk (7.0.0 vs the patch's 7.1.2 base)
- `xe_pcode_api.h` hunk: pure additions (FSC_* defines + FAN_CONTROL_POINT masks) → should apply clean.
- `xe_hwmon.c`: 13 hunks. Highest reject risk is anywhere the patch's CONTEXT lines reference 7.1.x-only power-limit code (7.1.2 added PL2 power fields our 7.0.0 lacks). Known integration points the patch changes:
  1. `xe_hwmon_pcode_read_fan_control` — signature gains a `u8 fan_num` param (was hardcoded param2=0); splits out a new `xe_hwmon_get_num_fans`. Context: the `XE_DG2` special-case block.
  2. `hwmon_info[]` — stock `HWMON_CHANNEL_INFO(fan, HWMON_F_INPUT, HWMON_F_INPUT, HWMON_F_INPUT)` → adds `HWMON_F_MAX` and a new `HWMON_CHANNEL_INFO(pwm, HWMON_PWM_INPUT | HWMON_PWM_ENABLE, ...)`. NOTE: must be applied to PRISTINE stock (our exploratory tree had `HWMON_F_TARGET` added — the apply script restores pristine first).
  3. `struct xe_hwmon_fan_info` — gains `fan_table[]`, `min_pwm`, `pwm_enable_mode`, point-count fields + `enum xe_fan_pwm_enable_mode`.
  4. is_visible / read / write dispatchers — new `hwmon_pwm` cases; new fan-curve attribute group (`pwm1_auto_point*`).
  5. Large blocks of NEW self-contained functions (write_user_fan_point, activate_user_fan_table, read_fan_control_info, etc.) — these are additions, low reject risk.
- If hunks reject: the fix is mechanical — open `xe_hwmon.c.patched`, copy the corresponding function/struct/dispatch changes onto our 7.0.0 `xe_hwmon.c` by hand. All the NEW code is identical; only the surrounding-context integration points differ.

## B60-specific (why this should work on e211, and the one thing to verify)
- Our B60 has `xe->info.has_fan_control = 1` (confirmed round 13) — the patch gates all fan sysfs on this, so the nodes WILL appear.
- The B60's `FAN_SPEED_CONTROL` PCODE responds (FSC_READ_NUM_FANS works → `fan1_input` visible), so the read side is proven.
- The patch uses PCODE writes, NOT the missing MEI `fan_control_8086_e211.bin` firmware. That firmware is only the autonomous STOCK table; the USER table is pure PCODE. So the B60 should get MANUAL fan control even without the firmware blob.
- THE ONE THING TO VERIFY on hardware: does the B60's PCODE accept `FSC_WRITE_FAN_TABLE`(0x1) + `FSC_WRITE_NUM_FAN_CONTROL_POINTS`(0x0) writes? (B580 does; same BMG PCODE — very likely yes.) After loading the patched module: `echo 1 > pwm1_enable; echo <temp> > pwm1_auto_point1_temp; echo <pwm> > pwm1_auto_point1_pwm; ...` and watch `fan1_input` respond. If the pcode writes ENXIO, then (and only then) it's firmware-gated.

## Safety reminders (from 2 crash incidents this investigation)
- Fresh boot = genuine stock `xe.ko.zst`. Remove any stale `xe.ko.zst` sibling before installing the plain `xe.ko` (modprobe prefers .zst — round-13 trap).
- Do NOT rapid-reload the module or hammer PCODE in tight loops (round-22 crash). One clean unbind→rmmod→modprobe→bind cycle; verify dmesg between steps.
- The patch's write path goes through the driver's forcewake-safe `xe_pcode_write` (correct), unlike raw `intel_reg`/out-of-lock MMIO (which caused the crash) — so using the patched sysfs is the SAFE way to drive the fan.
