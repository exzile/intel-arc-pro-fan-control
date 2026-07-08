# DKMS packaging (auto-rebuild on kernel updates) — BETA

Installing the patched `xe` via DKMS makes it **rebuild automatically on every
kernel update**, so fan/OC control survives upgrades. Without DKMS, a kernel
update replaces the module with the stock `xe` and you must re-run
`scripts/build-xe-module.sh` by hand.

> **BETA / validate first.** This patches an *in-tree* driver, which is unusual
> for DKMS (it needs the full kernel *source* + real config, not just headers —
> see [../docs/LINUX-BUILD.md](../docs/LINUX-BUILD.md)). Confirm it builds and
> **the GPU still binds after a reboot** on your machine before trusting it
> across updates.

## Prerequisites

```bash
sudo apt install dkms build-essential flex bison libssl-dev libelf-dev \
                 libdw-dev dwarves linux-headers-$(uname -r) \
                 linux-source-$(uname -r | cut -d- -f1) zstd
```

The matching **`linux-source-<base>`** must be installable for every kernel DKMS
will build against (that's what the recipe patches).

## Install

The DKMS source tree must be self-contained — it needs the fan patch and the OC
implementation files next to `dkms-build.sh`:

```bash
VER=0.1.0
sudo mkdir -p /usr/src/xe-arc-$VER
sudo cp dkms/dkms.conf dkms/dkms-build.sh /usr/src/xe-arc-$VER/
sudo cp -r patch        /usr/src/xe-arc-$VER/patch      # fan patch
sudo cp -r kernel       /usr/src/xe-arc-$VER/kernel     # xe_gt_oc.{c,h}

sudo dkms add     xe-arc/$VER
sudo dkms build   xe-arc/$VER
sudo dkms install xe-arc/$VER
sudo reboot
```

After reboot, verify the GPU bound (per docs/LINUX-BUILD.md). If the display is
zoomed/low-res, the module didn't bind:

```bash
sudo dkms remove xe-arc/$VER --all
sudo update-initramfs -u    # (or your distro equivalent)
sudo reboot                 # back to stock xe
```

## How it works

`dkms.conf` delegates the build to `dkms-build.sh <kernelver>`, which reproduces
the verified recipe (full source → patch → real config → version pin → build)
for the kernel DKMS is targeting, and drops `xe.ko` where DKMS expects it. The
built module installs to `/updates`, which the kernel loads in preference to the
stock `kernel/drivers/gpu/drm/xe`.

## Known limitations

* **Ubuntu/Debian-oriented.** The `linux-source-<base>` assumption is
  Debian-family; adapt the "kernel source" section of `dkms-build.sh` for Arch
  (ABS) / Fedora (`kernel-devel` ships enough source for many modules but the xe
  tracepoints may still need the full tree).
* **Big builds.** Each kernel update triggers a full `xe` compile (minutes).
* **Replaces an in-tree module.** More invasive than a normal add-on; the
  recovery path above matters. A bad build must never leave you unable to boot to
  a display — keep the stock-module restore handy.
