# Project notes — everything we tried and discovered

The running journal for this project: how Linux fan control **and full overclocking** were
reverse-engineered for the Intel Arc **Pro B60/B70** (Battlemage), what worked, what didn't, and
the hardware facts learned along the way. For the *how it works today* see
[OVERCLOCKING.md](OVERCLOCKING.md), [GPU-TUNING.md](GPU-TUNING.md), [GUI.md](GUI.md), and
[EVIDENCE.md](EVIDENCE.md); this file is the story + the traps.

Reference hardware: Arc **Pro B60** (`8086:e211`, Battlemage G21), PCI `0000:03:00.0`, Ubuntu 26.04 /
kernel 7.0.0, Linux `xe` driver.

---

## 1. Fan control (solved first)

**Goal:** custom fan curves on Linux, where stock `xe` exposes only read-only `fan1_input`.

- The enabling code is Intel's **kernel patch series 168027** (`drm/xe/hwmon`, Karthik Poosa) — not
  yet mainline. We bundle a CachyOS-adapted copy + automation (`scripts/apply_xefan.sh`).
- **Key finding:** the Arc **Pro** B60 does **not** need the missing MEI late-bind fan firmware
  (`fan_control_8086_e211.bin`) for *manual* control. That firmware only drives the autonomous
  *stock* table; the **user** table is pure PCODE `FAN_SPEED_CONTROL` (op `0x7d`), which the Pro
  card's PCODE accepts directly. Confirmed end-to-end on a real B60.
- Mechanism: per-point `FSC_WRITE_FAN_TABLE(0x1)` with `temp | speed<<8`, then
  `FSC_WRITE_NUM_FAN_CONTROL_POINTS(0x0)` to commit; `pwm1_enable` selects full(0)/manual(1)/
  auto(2). Up to 10 points. Details in [PORT_NOTES.md](PORT_NOTES.md).
- **Two crash incidents** taught the safety rules: never rapid-reload the module or hammer PCODE in
  tight loops; drive the fan through the driver's forcewake-safe `xe_pcode_write` sysfs, never raw
  MMIO. A fresh boot restores the genuine stock `xe.ko.zst` (modprobe prefers `.zst` — a real trap).

---

## 2. Overclocking (the hard one)

**Goal the user set:** "Windows can overclock this card; there must be a way to do the same on
Linux." We proved there is.

### 2.1 Dead ends first
- Assumed the Pro SKU had OC **fused off** — wrong.
- Chased the signed **MEI/GSC** firmware blobs and late-binding provisioning — those are a real
  gate (see §2.4) but not the path to runtime OC.
- Static analysis of the Windows KMD alone didn't reveal the sequence.

### 2.2 The breakthrough — DTrace the live Windows driver
We traced the **Windows** kernel driver (`igdkmdnd64`) while its tuning UI applied an overclock, and
captured the exact **PCODE mailbox** writes it issues.

- PCODE mailbox MMIO: `0x138124` = command/status, `0x138128` = DATA0, `0x13812c` = DATA1.
- Command word = `0x80000000(run) | (p2<<16) | (p1<<8) | op`. Our macro: `PCODE_MBOX(op, p1, p2)`.
- DTrace gotcha: the `+0x004c5310` offset form fails to parse — use a glob probe
  `fbt:igdkmdnd64:*004c5310:entry`. Anonymous boot tracing isn't wired on Windows DTrace; we used
  live driver-reload capture instead.

### 2.3 The missing transaction (the actual unlock)
The VF-curve write is a **bracketed transaction** the vendor driver sends and stock `xe` omits:

```
PCODE_MBOX(0x5f, 2, 0)              begin write session      <-- THE MISSING PIECE
PCODE_MBOX(0x5d, 0xa, 3) DATA0=P    write point P = (mV<<8 | index), x85
PCODE_MBOX(0x5d, 0xb, 3)            end / finalize
```

Without the `0x5f/2` **begin**, every per-point write returns `-EPROTO` (mailbox "illegal
subcommand"). That single missing command is why VF-curve tuning was "impossible" on Linux — the
capability read (`0x5c`) and per-point reads are identical to Windows; only the begin/write/end was
never issued. We added it in a small `xe_gt_oc` kernel patch (`kernel/xe_gt_oc.c`).

### 2.4 The opcode map (op `0x5e` domain multiplexing)
Beyond the VF curve, op `0x5e` with `p2=domain` exposes more knobs (sub-op via `p1`: 5=read,
6=write, 8=commit):

| domain | knob | notes |
|---|---|---|
| `0x17` | GDDR6 **memory speed** (Mbps) | write d0=Mbps, commit; reads `-71` until a value is staged |
| `0x49` | **temperature limit** (°C) | same read/write/commit shape |
| `0x30` | **VR params** (voltage regulator) | **firmware-locked** — see below |

Confirmed opcode **names** by cross-checking upstream kernel headers (`xe_pcode_api.h`):
`PCODE_LATE_BINDING=0x5c`, `PCODE_POWER_SETUP=0x7c`, `PCODE_THERMAL_INFO=0x25`,
`FAN_SPEED_CONTROL=0x7d`, `DGFX_PCODE_STATUS=0x7e`, `XEHP_PCODE_FREQUENCY_CONFIG=0x6e`, mailbox
`GEN6_PCODE_MAILBOX=0x138124`. Our reverse-engineered names all matched.

### 2.5 The VR lock is real (and the kernel documents it)
The late-binding capability read (`0x5c`) returns **`0x9`** on our card. The kernel's own bit names:
`V1_FAN_SUPPORTED=bit0`, `VR_PARAMS_SUPPORTED=bit3`, `V1_FAN_PROVISIONED=bit16`,
`VR_PARAMS_PROVISIONED=bit19`. So `0x9` = fan+VR **supported** but **not provisioned** — a
signed-firmware provisioning gate, genuinely unreachable from Linux runtime. Direct **voltage**
control (the VR domain `0x30`) is therefore off the table without provisioning; the VF curve
(undervolt/overvolt of the existing curve) is what we get, which matches what the Windows app
exposes.

### 2.6 Hardware constraints discovered on the metal
Tested by replaying writes on a real B60:

- **The VF curve must be monotonic** (voltage non-decreasing with frequency). A *partial* write
  that dips a point below its predecessor is silently clamped back up by PCODE (`set 84 1000`
  no-op'd). A *full* uniform shift (`offset`) that keeps monotonicity is accepted.
- **The top points (~indices 80–84) sit on a fixed Vmax rail at 1035 mV** and can't be individually
  lowered. The GUI mirrors both rules so the on-screen preview is exactly what lands.
- **Live domain enumeration is unsafe.** A blind 256-domain scan (even single reads of `0x70`/`0x71`/
  `0x72`) **wedged the PCODE mailbox → GPU hang → reboot**. `0x6e` and the `0x70`-family are real but
  HBM/PVC-specific or telemetry that expect a precise call sequence. Conclusion: the Windows app is
  the complete *safely reachable* OC surface; there are no hidden runtime knobs.

### 2.7 Online research validation
Confirmed against the Linux kernel `xe_pcode` docs/headers, the `xe_pcode_fwctl` patch (a generic
userspace PCODE path Intel is upstreaming — worth watching), IGCL (Intel Graphics Control Library
`ctlOverclock*`), and B580 community tuning: upstream `xe` only implements the `0x5c` **reads**; the
`0x5d`/`0x5e` **writes** are Intel-private late-binding ops never upstreamed — exactly the gap we
filled. The consumer B580 (same Battlemage silicon) has the identical OC surface; IGCL's full OC
parameter set is entirely covered by our implementation.

---

## 3. Board-level investigation (asked, answered "no gain")
Considered reading the on-board ICs' datasheets (VRM controller, GDDR6) for extra knobs. Verdict:
GDDR6 mode-registers and the VR controller are **slaves of the GPU's internal management** (memory
PHY / package-internal SVID bus), not host-addressable — the datasheets would confirm ceilings, not
reveal a bypass. The one tantalizing exception (direct PMBus to the VR controller) is the same
firmware-locked VR domain **and** the highest brick-risk action on the board. No gain; not pursued.

---

## 4. Telemetry findings (what xe actually exposes on Linux)

**Available (real hardware readings):**
- Clocks: `cur_freq`, `act_freq`, `min/max_freq`, `rpn/rp0/rpa/rpe_freq` (freq0).
- **Power draw** is *derivable* — `energy1_input` (whole card) and `energy2_input` (GPU package) are
  cumulative µJ counters; watts = Δenergy/Δtime. `power1_input` is empty; `power1_cap`/`power1_crit`
  are the limits. Idle ≈ 31 W on our B60.
- Fan: `fan1_input` (rpm), `fan1_max`, `pwm1` (duty), `pwm1_enable` (mode).
- Temps: `temp*_label`/`_input`/`_crit` — mains `pkg`/`vram`/`mctrl`/`pcie` + **12** `vram_ch_0..11`.
- **Throttle reasons**: `freq0/throttle/reason_*` (pl1/pl2/pl4, prochot, ratl, thermal, vr_tdc) as
  0/1 flags → "Power/Temperature/Voltage limited" indicators.
- **VRAM used/total** — **only in root-only debugfs** `tile0/vram_mm` (`size:`/`usage:` bytes). Not
  in sysfs, which is why it looked missing. We expose *only* those two numbers via a tiny root
  service (`xe-gpu-vramd` → `/run/xe-gpu-vram-<bdf>`) so the unprivileged GUI can show it without
  weakening debugfs or prompting every poll.

**NOT available on Linux xe** (present in Windows Arc Control, absent here — documented, never
faked): GPU/VRAM **utilization %**, VRAM **used/size** outside debugfs, VRAM **bandwidth/frequency**,
per-engine **render/compute/media %**, and all **frame-latency / FPS** metrics (those come from the
present pipeline, not the GPU).

---

## 5. The delivered toolkit

- **Kernel:** `xe_gt_oc` patch adds `oc/vf_curve`, `oc/mem_speed`, `oc/temp_limit` sysfs (the VF
  begin/write/end transaction + domains `0x17`/`0x49`). Fan patch = series 168027.
- **CLI:** `xe-gpu-oc` (offset/curve/mem/temp/reset/**profile save·load·list**/boot), `xe-gpu-tune`
  (power/clock/profile), `xe-fan-curve`, `xe-gpu-stress` (fan-guarded stability test + `DRI_PRIME`
  pinning), `xe-gpu-temps`, `xe-gpu`, `xe-gpu-vramd`. All honour `ARC_GPU_BDF` for **multi-GPU**.
- **GUI:** GTK4/libadwaita `xe-gpu-gui` — Dashboard (Specifications + animated metric/temperature
  tiles with sparklines, rings, custom vector icons, a persistent metrics filter, and a **GPU
  selector** for multi-card boxes), Fan Control (draggable curve + Auto/Max), Overclock (VF-curve
  editor, presets, save/load profiles, stability test). See [GUI.md](GUI.md).
- **Persistence:** the card resets to stock on cold boot; systemd `boot` services re-apply saved
  choices. `install.sh` installs everything from the checkout (`git pull && sudo bash install.sh`).

---

## 5b. Second card — Arc Pro B70 (Battlemage G31, `e223`)

Adding a **B70** next to the B60 surfaced two die-specific facts — full writeup in
[B70-G31-MULTI-GPU.md](B70-G31-MULTI-GPU.md):

- **It wouldn't bind** on the Above-4G-less Z370 board: the B70 POSTs a **32 GB** VRAM BAR that can't
  map below 4 GB (enabling Above-4G instead starved the **NVMe boot drive** → unbootable). Fix:
  shrink the BAR to **256 MB** via its Resizable-BAR control reg, then **kexec** (which skips the
  PCIe reset so the setting sticks). Made persistent by `xe-b70-rebar.service` (one-shot,
  loop-guarded) + cmdline `pci=realloc=on xe.vram_bar_size=256 xe.max_vfs=0`.
- **The G31 rejects the OC-write PCODE surface.** Fan control (op `0x7d`) and power/clock sysfs work,
  but `0x5d` (VF curve), `0x5f/2` (begin) and `0x5e` (mem/temp) all return **`-71` (EPROTO)** where
  the B60 accepts them — while the general mailbox works (`0x5c` cap reads the **same** `0x9`). So
  the B70's OC uses a **different opcode set / firmware path**; it needs its own DTrace-on-Windows RE
  pass (the B60 method doesn't port). A debug `oc/pcode_probe` sysfs was added to `xe_gt_oc.c` as the
  bench for validating candidate sequences.

---

## 6. Traps worth remembering
- The `xe_gt_oc` GUI's CSS is a Python **bytes literal** (`b"""…"""`) → **ASCII only**; an em-dash or
  ✓/🔥 inside it is a `SyntaxError`. (Bit us three times.)
- Reading `oc/mem_speed`/`oc/temp_limit` returns `-71` until a value is staged (fresh boot = hw
  default) — the tools fall back to the stock constant for display.
- `FeatureTask`-style helpers that exit non-zero on success (robocopy = 1) discard their JSON.
- On multi-GPU, `pkexec` strips the environment — pass `ARC_GPU_BDF` via `pkexec /usr/bin/env`.
- Building the dashboard from a full `snapshot()` on the UI thread stalls ~1 s (first GPU wake +
  per-channel temp reads) — build the registry from the cheap `probe()` (labels + rp0 + VRAM total).

---

*Not affiliated with or endorsed by Intel. Everything here was derived by tracing our own hardware
and cross-checking public kernel sources; use at your own risk (see [EVIDENCE.md](EVIDENCE.md) for
exactly what was tested).*
