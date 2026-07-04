# GPU power & clock tuning

`xe-gpu-tune` uses only driver-exposed sysfs — no patch, no PCODE/MMIO poking. The driver clamps
all values to valid ranges, so it's safe.

## Commands
```bash
sudo xe-gpu-tune show
sudo xe-gpu-tune set --power-w 150 --clk-max 2000 --clk-min 400
sudo xe-gpu-tune reset          # hardware defaults
```
Persistent config: `/etc/xe-gpu-tune.conf` (applied at boot by `xe-gpu-tune.service`).

## What the knobs do (Arc Pro B60 ranges shown)
- **`--clk-min` (MHz)** — idle clock floor. Firmware default floors it at **1200 MHz** even at idle;
  set **400** (hardware min) so the GPU drops to 400 at idle → real idle power/heat saving, still
  boosts to max under load. **Pure win** — this repo enables it by default.
- **`--clk-max` (MHz)** — clock ceiling (hardware max `rp0` = 2400). Lower it (e.g. 2000) for less
  heat/noise/power under load, at some peak-perf cost. You can't exceed `rp0`.
- **`--power-w` (watts)** — package power cap (`power1_cap`; default 200 W, crit 400 W). Lowering it
  (e.g. 150 W) is the most effective single lever for a cooler/quieter sustained-AI card.

## Which sysfs it uses
- Clocks: `/sys/class/drm/cardN/device/tile0/gt0/freq0/{min,max,cur,rp0,rpn,rpe}_freq` (MHz)
- Power: `/sys/class/hwmon/hwmonN/power1_cap` (microwatts), `power1_crit`
(the script auto-finds the xe card and hwmon by driver/name, so `N` doesn't matter.)

## Not included (would need reverse-engineering + a patch)
- **Temperature limit / throttle point** (the Windows "Temperature Limit" slider) — a PCODE thermal
  write; same approach as the fan patch.
- **Undervolt** (voltage-frequency curve) — best perf-per-watt but needs voltage-opcode RE and an
  RFC to the xe list; carries real stability risk.
