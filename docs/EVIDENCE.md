# Test evidence — Arc Pro B60

Exactly what was verified, on real hardware.

## System
- **Card:** Intel Arc Pro B60 — Battlemage G21 — `8086:e211`, subsystem ASRock `1849:6023`
- **OS/Kernel:** Ubuntu 26.04 LTS, `7.0.0-27-generic`, `xe` driver
- **Patch:** Intel series 168027 (CachyOS-7.1.2 adaptation), applied to stock `xe_hwmon.c` +
  `xe_pcode_api.h` with `patch -p1 --fuzz=5` (0 rejected hunks), built as out-of-tree `xe.ko`.

## Fan — READ path
Driver reads the card's stock fan table from the `FAN_SPEED_CONTROL` PCODE mailbox:
```
fan1_max = 4980 RPM
stock curve (pwm1_auto_pointN_temp -> _pwm):
  59C->0  60C->51  65C->77  70C->102  75C->153  79C->204  84C->255  (255 to top)
```

## Fan — WRITE path (all three modes drive the fan)
- `pwm1_enable=0` (full speed): `0 -> 976 -> 3799 -> 4852 -> 4940 RPM` (≈ fan1_max).
- `pwm1_enable=1` + custom 10-point `pwm1_auto_point*` curve: fan holds ~2400 RPM at 48 °C and
  actively cooled the GPU **48 °C -> 44 °C**.
- Direct `pwm1` writes: `pwm1=180` -> ~3540 RPM, `pwm1=60` -> ~1171 RPM.
- No `dmesg` faults during any test.

## Autonomous behaviour (why the MEI firmware is only needed for auto)
In `pwm1_enable=2` (auto-stock), the fan stays at **0 RPM at 60 °C** — the Pro card has no
autonomous fan firmware on Linux. But that does **not** block manual control: the user-table
PCODE writes work regardless. So an always-active user curve (this toolkit's systemd service, or
CoolerControl) is what makes the fan respond to heat.

## GPU tuning (driver sysfs, no patch)
- `min_freq` 1200 -> 400 MHz: `cur_freq` immediately dropped to 400 at idle (idle power/heat saving;
  still boosts to 2400 under load).
- `max_freq` 2400 -> 1800 MHz: ceiling enforced.
- `power1_cap` 200 -> 150 W: applied.
- Hardware range: `rpn`(min) 400, `rp0`(max) 2400 MHz; power cap default 200 W, crit 400 W.

## Notes
- B70 (`8086:e223`) is expected to work identically (same Battlemage PCODE + `has_fan_control`) but
  was not tested here — reports welcome.
