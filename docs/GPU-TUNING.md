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

## What actually raises performance (and what doesn't)

Overclocking a Battlemage Arc is mostly about *whether there's any headroom at all* — on many cards
there isn't. Read your own card before chasing gains (the GUI's **Stock bench** + comparison table
does this for you):

- **A positive voltage offset does NOT make the GPU faster.** Clock speed is set by the frequency
  table and capped by the power/thermal limits — not by voltage. Adding volts at the same frequency
  just burns more power and heat, which can push you *into* throttling and **lower** your clocks. A
  positive offset at stock frequency is close to all-cost/no-benefit. Voltage only helps paired with
  a real frequency increase, or as a **negative** offset (undervolt).
- **`rp0` is a hard wall.** `max_freq` can never exceed `rp0_freq` (the firmware max boost). If
  `max_freq == rp0_freq` and `act_freq` already sits at `rp0` under load, the core is *already
  maxed* — there is no clock headroom, and raising the power cap won't unlock more (nothing to
  unlock above `rp0`). Intel's firmware doesn't expose raising `rp0` on Battlemage.
- **Memory headroom is often tiny.** GDDR6 that runs, say, 19 Gbps stock may hard-crash the whole
  machine at 20. Bump `mem_speed` in *small* steps and always keep the LLM coherence check on — a
  bad memory OC corrupts data silently (high tok/s, garbage output) rather than hanging.
- **The one lever on a maxed-out card is an *undervolt* — for efficiency, not speed.** If you're
  already holding `rp0` within the power budget, a negative voltage offset keeps the same clocks
  while running **cooler, quieter, and at lower power**. Set it in the Overclock tab; verify with the
  stability test.

For LLM inference specifically: **decode** tok/s (the words-per-second you feel) is memory-bandwidth
bound → helped only by a *memory* OC (if the card allows one). **Prefill** is compute bound → helped
by higher core clock (if you have `rp0` headroom). On a card with neither, expect "stock ± noise" —
run stock 2–3× to see the noise band before trusting any single comparison.

## Also available via the `xe_gt_oc` patch (see OVERCLOCKING.md)
These used to require reverse-engineering and are now implemented:
- **Temperature limit / throttle point** — `.../gt0/oc/temp_limit` (`xe-gpu-oc temp …`).
- **Voltage-frequency curve / undervolt** — `.../gt0/oc/vf_curve` (`xe-gpu-oc offset|curve …`).
- **VRAM memory-speed overclock** — `.../gt0/oc/mem_speed` (`xe-gpu-oc mem …`).
