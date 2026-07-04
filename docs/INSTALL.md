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
```bash
# from this repo:
sudo bash scripts/apply_xefan.sh                 # DRY RUN — check all hunks apply
sudo APPLY=1 bash scripts/apply_xefan.sh         # apply patch + build xe.ko
```
Install the module (remove the stale compressed one first — `modprobe` prefers `.zst`):
```bash
KREL=$(uname -r); XE=/home/$USER/linux-source-$(echo $KREL|sed -E 's/-[0-9]+-.*//')/drivers/gpu/drm/xe
MODDIR=/lib/modules/$KREL/kernel/drivers/gpu/drm/xe
sudo mv $MODDIR/xe.ko.zst /root/xe.ko.zst.stock-bak 2>/dev/null || true
sudo cp $XE/xe.ko $MODDIR/xe.ko && sudo depmod -a
```
Reload (one clean cycle — PCI unbind avoids the gnome-shell FD-holds-module trap):
```bash
sudo sh -c 'echo 0000:03:00.0 > /sys/bus/pci/drivers/xe/unbind'   # your PCI id: lspci -d 8086:e211
sudo rmmod xe && sudo modprobe xe
```
Verify:
```bash
ls /sys/class/hwmon/hwmon*/pwm1_enable        # should exist now (find the one whose name==xe)
```

## 3. Install helpers + persistence
```bash
sudo install -m755 scripts/xe-fan-curve.sh   /usr/local/bin/xe-fan-curve
sudo install -m755 scripts/xe-gpu-tune.sh    /usr/local/bin/xe-gpu-tune
sudo install -m755 scripts/xe-gpu-temps.sh   /usr/local/bin/xe-gpu-temps
sudo install -m755 scripts/xe-gpu.sh         /usr/local/bin/xe-gpu
sudo install -m755 scripts/xe-fan-rebuild.sh /usr/local/sbin/xe-fan-rebuild
sudo install -m755 kernel-hook/zz-xe-fan-rebuild /etc/kernel/postinst.d/zz-xe-fan-rebuild
sudo mkdir -p /usr/local/share/xe-fan
sudo cp patch/xe-fan-control-168027-cachyos-7.1.2.patch /usr/local/share/xe-fan/
sudo cp systemd/etc/xe-fan-curve.conf systemd/etc/xe-gpu-tune.conf /etc/
sudo cp systemd/xe-fan-curve.service systemd/xe-gpu-tune.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xe-fan-curve.service xe-gpu-tune.service
```

## 4. Verify
```bash
sudo xe-fan-curve show     # mode 1, your curve, RPM tracking temp
sudo xe-gpu-tune show      # idle clock 400, full max
```

## Uninstall / revert
- Boot the stock kernel entry, or `sudo systemctl disable --now xe-fan-curve.service xe-gpu-tune.service`
  and restore the stock `xe.ko.zst` you backed up, then reload.
- A fresh boot after removing the patched `xe.ko` returns to genuine stock (read-only fan).
