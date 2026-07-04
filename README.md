# Intel Arc **Pro** Battlemage — Linux fan control & GPU tuning

Working **custom fan curves**, **power/clock tuning**, and **kernel-update resilience** for the
**Intel Arc Pro B60 / B70** (Battlemage) on Linux, where the stock `xe` driver exposes only
read-only fan RPM.

> **Key finding:** the Arc **Pro** B60 (`8086:e211`) does **not** need the missing MEI late-bind
> fan firmware for *manual* control. Intel's fan-control patch (series 168027) programs the user
> fan table straight over the `FAN_SPEED_CONTROL` PCODE mailbox, and the Pro card's PCODE accepts
> it directly. Confirmed working end-to-end on a real B60 — see [docs/EVIDENCE.md](docs/EVIDENCE.md).

Related upstream issue: [intel/compute-runtime #885](https://github.com/intel/compute-runtime/issues/885).

## What works (verified on Arc Pro B60, `8086:e211`, kernel 7.0.0 / Ubuntu 26.04)

| Capability | Stock `xe` | With this toolkit |
|---|---|---|
| Read fan RPM / temps | ✅ | ✅ |
| Write fan speed (`pwm1`) | ❌ | ✅ |
| 10-point fan curve (`pwm1_auto_point*`) | ❌ | ✅ |
| CoolerControl GUI management | read-only | ✅ manual / graph |
| GPU power cap (TDP) | ✅ sysfs | ✅ + persistent helper |
| GPU clock min/max limits | ✅ sysfs | ✅ + persistent helper |
| Idle power/heat optimization | ❌ (idles at 1200 MHz) | ✅ (idles at 400 MHz) |
| All-sensor temp/health monitor | raw sysfs | ✅ `xe-gpu-temps` (table/watch/json) |
| Single-command status dashboard | — | ✅ `xe-gpu` (fan+clocks+power+temps) |
| Survives reboots | — | ✅ systemd |
| Survives kernel updates | — | ✅ auto-rebuild hook |

## How it works

- **Fan control** is Intel's kernel patch **series 168027** (`drm/xe/hwmon`, by Karthik Poosa) —
  not yet in mainline. This repo bundles the CachyOS-7.1.2-adapted patch (which also applies to
  Ubuntu 7.0.0 with fuzz) plus automation to build it as an out-of-tree `xe.ko`, and userland
  helpers on top. Fan curve = `FAN_SPEED_CONTROL` PCODE: per-point `FSC_WRITE_FAN_TABLE` (0x1) with
  `temp | speed<<8`, then `FSC_WRITE_NUM_FAN_CONTROL_POINTS` (0x0) to commit; `pwm1_enable` selects
  full-speed(0)/manual(1)/auto-stock(2).
- **Power/clock tuning** uses only driver-exposed sysfs (`.../gt0/freq0/*`, hwmon `power1_cap`) —
  no patch, no PCODE poking, safe.

## Quick start (Ubuntu / Debian-ish)

```bash
# 0. prereqs: matching kernel source + build tools
sudo apt install linux-source-$(uname -r | sed -E 's/-[0-9]+-.*//') build-essential libdw-dev
# extract it to /home/$USER/linux-source-<ver> (see docs/INSTALL.md) with .config + Module.symvers

# 1. build & install the patched xe module (restores pristine source, applies patch, builds)
sudo bash scripts/apply_xefan.sh                 # dry-run
sudo APPLY=1 bash scripts/apply_xefan.sh         # apply + build
# install the module + reload — see docs/INSTALL.md

# 2. install the userland helpers + persistence
sudo install -m755 scripts/xe-fan-curve.sh  /usr/local/bin/xe-fan-curve
sudo install -m755 scripts/xe-gpu-tune.sh   /usr/local/bin/xe-gpu-tune
sudo install -m755 scripts/xe-gpu-temps.sh  /usr/local/bin/xe-gpu-temps
sudo install -m755 scripts/xe-gpu.sh        /usr/local/bin/xe-gpu
sudo install -m755 scripts/xe-fan-rebuild.sh /usr/local/sbin/xe-fan-rebuild
sudo install -m755 kernel-hook/zz-xe-fan-rebuild /etc/kernel/postinst.d/zz-xe-fan-rebuild
sudo cp systemd/etc/*.conf /etc/
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xe-fan-curve.service xe-gpu-tune.service
```

Full step-by-step (with the module install/reload, `.zst` gotcha, and verification):
**[docs/INSTALL.md](docs/INSTALL.md)**.

## Usage

```bash
# one-stop dashboard + front-end (wraps the tools below)
xe-gpu                 # status: card + clocks + power + fan + temps in one view
xe-gpu watch           # live dashboard
xe-gpu fan set 45:80 55:130 65:180 75:220 85:255   # -> xe-fan-curve
xe-gpu tune set --power-w 150                        # -> xe-gpu-tune
xe-gpu temps                                         # -> xe-gpu-temps

# fan
sudo xe-fan-curve show
sudo xe-fan-curve set 45:80 55:130 65:180 75:220 85:255   # temp°C : pwm(0-255)
sudo xe-fan-curve auto        # hand back to stock auto table
sudo xe-fan-curve max         # full speed

# power / clocks
sudo xe-gpu-tune show
sudo xe-gpu-tune set --power-w 150 --clk-max 2000 --clk-min 400
sudo xe-gpu-tune reset

# temperatures / health (read-only, no patch needed)
xe-gpu-temps            # table of every sensor + limits, fan, power
xe-gpu-temps watch 2    # live refresh every 2s
xe-gpu-temps json       # machine-readable (for scripts / dashboards)
```

- GUI fan curves via **CoolerControl** — see [docs/COOLERCONTROL.md](docs/COOLERCONTROL.md).
- GPU tuning details — see [docs/GPU-TUNING.md](docs/GPU-TUNING.md).
- Persistent config: `/etc/xe-fan-curve.conf`, `/etc/xe-gpu-tune.conf`.

## Kernel updates

The patched `xe.ko` is replaced by the stock driver on a kernel update. The included
`/etc/kernel/postinst.d/zz-xe-fan-rebuild` hook (calling `xe-fan-rebuild`) rebuilds it
automatically **when the matching `linux-source` is installed**. Major kernel jumps may need
`apt install linux-source-<newver>` and a patch re-fuzz. (True DKMS is not possible — `xe` can't
build against headers-only; it needs the full kernel source + i915 siblings.)

## Contributing upstream

The real fix is series 168027 landing in mainline. The highest-impact thing you can do is add a
**`Tested-by`** for your card to that series on `intel-xe@lists.freedesktop.org`, and confirm on
compute-runtime #885. Templates + full workflow: [contrib/](contrib/).

## Credits & license

- **Fan-control kernel patch**: Intel — series 168027, *Karthik Poosa* (`intel-xe@lists.freedesktop.org`). GPL-2.0. Bundled here under `patch/` with its kernel lineage intact.
- **B580 groundwork + CachyOS patch adaptation**: [PerkyZZ999/XeDriver_FanPatch](https://github.com/PerkyZZ999/XeDriver_FanPatch).
- **This repo's automation, helpers, tuning, and Pro-card (B60/B70) enablement**: see LICENSE (MIT for the scripts). The bundled kernel patch remains GPL-2.0.

*Not affiliated with or endorsed by Intel. Use at your own risk — see [docs/EVIDENCE.md](docs/EVIDENCE.md) for exactly what was tested.*
