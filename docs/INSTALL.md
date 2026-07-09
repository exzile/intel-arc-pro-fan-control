# Install — step by step

Tested on Ubuntu 26.04 / kernel 7.0.0-27-generic + Arc Pro B60. Adapt paths for your distro.

> ⚠️ You are building and loading a modified GPU kernel module. Do it on a machine you can recover
> (a fresh boot loads the genuine stock module). Don't rapid-reload the module or poke PCODE/MMIO
> by hand — use the driver sysfs the scripts use.

## 1. Prerequisites
```bash
sudo apt install build-essential libdw-dev git   # build deps (libdw-dev for gendwarfksyms)
BASE=$(uname -r | sed -E 's/-[0-9]+-.*//')        # e.g. 7.0.0
sudo apt install linux-source-$BASE               # matching kernel source
```
Extract the source tree and prime it to build modules for your running kernel:
```bash
cd /home/$USER
tar -xjf /usr/src/linux-source-$BASE.tar.bz2
cd linux-source-$BASE
cp /boot/config-$(uname -r) .config
cp /lib/modules/$(uname -r)/build/Module.symvers .
make olddefconfig && make modules_prepare
```
The scripts expect the tree at `/home/$USER/linux-source-$BASE` (or `/usr/src/...`).

## 2. Build & install the patched module

The patched `xe` driver (fan + OC sysfs) is built for your **running kernel** by
the verified script. It uses the kernel's real config + version (from the headers
package) so the module binds correctly — do **not** hand-edit `.config` or force
the `vermagic`, which produces a module that loads but silently won't bind (see
[LINUX-BUILD.md](LINUX-BUILD.md) for the full explanation and a manual walkthrough).

```bash
# build + verify only — safe, does not touch the running module:
sudo bash scripts/build-xe-module.sh --build-only

# build, back up the stock module, install, then reboot to activate:
sudo bash scripts/build-xe-module.sh
sudo reboot
```

Activate with a **reboot** (a live `rmmod`/`modprobe` swap is unsafe while the
desktop compositor holds the GPU). The script prints the exact post-reboot
checks; the key one is that the GPU still binds:

```bash
lspci -k -s "$(lspci -Dn | awk '/8086:e2(11|23)/{print $1; exit}')" | grep 'in use'   # -> xe
ls /sys/class/drm/card*/device/hwmon/hwmon*/pwm1_enable                                # fan sysfs
```

> To survive **kernel updates** automatically (rather than rebuilding by hand each
> time), use the DKMS package instead — see [../dkms/README.md](../dkms/README.md).

## 3. Install helpers + persistence
```bash
sudo install -m755 scripts/xe-fan-curve.sh   /usr/local/bin/xe-fan-curve
sudo install -m755 scripts/xe-gpu-tune.sh    /usr/local/bin/xe-gpu-tune
sudo install -m755 scripts/xe-gpu-temps.sh   /usr/local/bin/xe-gpu-temps
sudo install -m755 scripts/xe-gpu.sh         /usr/local/bin/xe-gpu
sudo install -m755 scripts/xe-fan-rebuild.sh /usr/local/sbin/xe-fan-rebuild

# optional native desktop GUI (GTK4 + libadwaita)
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1   # runtime deps
# (python3-gi-cairo is required for the fan-curve graph to render)
sudo install -m755 gui/xe-gpu-gui.py /usr/local/bin/xe-gpu-gui
install -m644 gui/xe-gpu-gui.desktop ~/.local/share/applications/   # appears as "Arc GPU Dashboard"
update-desktop-database ~/.local/share/applications 2>/dev/null || true
# controls (fan/power/clock) use pkexec, so the helpers must be installed too
sudo install -m755 kernel-hook/zz-xe-fan-rebuild /etc/kernel/postinst.d/zz-xe-fan-rebuild
sudo mkdir -p /usr/local/share/xe-fan
sudo cp patch/xe-fan-control-168027-cachyos-7.1.2.patch /usr/local/share/xe-fan/
sudo cp systemd/etc/xe-fan-curve.conf systemd/etc/xe-gpu-tune.conf /etc/
sudo cp systemd/xe-fan-curve.service systemd/xe-gpu-tune.service \
        systemd/xe-gpu-oc.service systemd/xe-gpu-oc-confirm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xe-fan-curve.service xe-gpu-tune.service \
                            xe-gpu-oc.service xe-gpu-oc-confirm.service
```

> **Overclock safety (failed-boot watchdog).** `xe-gpu-oc.service` re-applies your
> saved overclock at boot, but arms a marker first; `xe-gpu-oc-confirm.service`
> clears it ~45 s into a healthy desktop. If an overclock ever hangs the box
> before then, the next boot sees the un-cleared marker and **automatically
> disables the overclock** (moves `/etc/xe-gpu-oc.conf` to `.conf.crashed`) so you
> can never get stuck in a boot loop. Re-apply from the GUI once you adjust it.

> **No repeat password prompts (polkit).** `install.sh` installs a scoped polkit
> rule (`/etc/polkit-1/rules.d/49-xe-gpu.rules`) so the GPU-control helpers run
> **without a `pkexec` prompt** for a locally logged-in admin (`sudo`/`wheel`,
> active session). It's limited to those specific binaries — package installs and
> everything else still prompt, and SSH/remote sessions are never covered. To keep
> per-action prompts instead, just `sudo rm` that file. Needs polkit ≥ 0.106 (the
> JS-rules `/etc/polkit-1/rules.d` directory); older systems simply keep prompting.

> **Optional benchmark setup.** The GUI's opt-in *Benchmark* (FPS / VRAM bandwidth /
> compute / real LLM tokens-per-second) installs its tools on first use, or up front
> with `bash scripts/setup-llm-benchmark.sh` — it adds `clpeak` + Intel OpenCL and a
> self-contained Python 3.12 env under `~/ovbench` with a small INT4 model. Skip it
> and the stability test still runs; you just won't get the benchmark metrics.

## 4. Verify
```bash
sudo xe-fan-curve show     # mode 1, your curve, RPM tracking temp
sudo xe-gpu-tune show      # idle clock 400, full max
```

## Uninstall / revert
- Boot the stock kernel entry, or `sudo systemctl disable --now xe-fan-curve.service xe-gpu-tune.service`
  and restore the stock `xe.ko.zst` you backed up, then reload.
- A fresh boot after removing the patched `xe.ko` returns to genuine stock (read-only fan).
