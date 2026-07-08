# Building the patched `xe` driver (fan control + overclock)

Fan control and the VF-curve overclock require a **patched `xe` kernel module**
(the stock driver doesn't expose the `pwm1_*` or `gt0/oc/vf_curve` sysfs). This
guide covers building and installing it on **your running kernel**.

> **TL;DR** — run the automated script:
> ```bash
> sudo bash scripts/build-xe-module.sh          # build + install, then reboot
> sudo bash scripts/build-xe-module.sh --build-only   # build + verify only
> ```
> The rest of this document explains what it does and how to do it by hand.

---

## What you get

* `…/hwmon/hwmonN/pwm1_enable`, `pwm1`, `pwm1_auto_pointN_{temp,pwm}` — fan curve
* `…/tile0/gt0/oc/vf_curve`, `mem_speed`, `temp_limit` — overclock / undervolt
* The GUI's **Overclock** tab appears (it's gated on `vf_curve` existing)

## Prerequisites

Install the toolchain, this kernel's **headers** *and* **full source**, and two
non-obvious deps — `libdw-dev` (provides `<dwarf.h>` for `gendwarfksyms`) and
`dwarves` (provides `pahole` for BTF):

```bash
# Ubuntu / Debian
sudo apt install build-essential flex bison libssl-dev libelf-dev \
                 libdw-dev dwarves linux-headers-$(uname -r) \
                 linux-source-$(uname -r | cut -d- -f1) zstd
```

| Distro | source / headers |
|---|---|
| Ubuntu / Debian | `linux-source-<ver>` + `linux-headers-$(uname -r)` |
| Arch / CachyOS | `linux-headers` (per-kernel variant), source via ABS |
| Fedora | `kernel-devel-$(uname -r)` |

---

## The two rules that matter (why a naive build fails)

A wrong build **loads but silently won't bind the GPU** — the display falls back
to `simpledrm` at low resolution, with **no error in dmesg**. Two causes, both
avoidable:

1. **Build from the FULL source tree, not against the headers dir.**
   `xe`'s tracepoints use a relative `TRACE_INCLUDE_PATH`
   (`../../drivers/gpu/drm/xe/xe_trace.h`) that only resolves in-tree. A
   `make -C /usr/src/linux-headers-$(uname -r) M=<xe> modules` build **fails**
   with `fatal error: …/xe_trace.h: No such file or directory`.

2. **Take the config + version from the running kernel — don't edit either.**
   Editing `.config` or force-overriding the `vermagic` produces an
   ABI-incompatible module that loads but won't bind. Instead copy the real
   `.config`, `utsrelease.h`, `kernel.release`, and `Module.symvers` **from the
   headers package** so vermagic and ABI come out correct automatically.

If you ever find yourself forcing the `vermagic` string, **stop** — your build
environment doesn't match the kernel and the ABI is probably wrong too.

---

## Manual build (what the script automates)

```bash
KREL=$(uname -r); BASE=${KREL%%-*}
HDR=/usr/src/linux-headers-$KREL
SRC=/usr/src/xe-arc-src/linux-source-$BASE
XE=$SRC/drivers/gpu/drm/xe

# 1. extract the full kernel source
sudo mkdir -p /usr/src/xe-arc-src
sudo tar -xf /usr/src/linux-source-$BASE/linux-source-$BASE.tar.* -C /usr/src/xe-arc-src

# 2. apply the fan patch
( cd "$SRC" && sudo patch -p1 --fuzz=5 -i /path/to/repo/patch/xe-fan-control-*.patch )

# 3. apply the OC feature (copy impl + three hooks)
sudo cp /path/to/repo/kernel/xe_gt_oc.{c,h} "$XE/"
#   Makefile:  add `xe_gt_oc.o \` next to `xe_gt_freq.o \`
#   xe_gt.c:   add `#include "xe_gt_oc.h"` and, in xe_gt_init(), an
#              `xe_gt_oc_init(gt)` call right after the xe_gt_freq_init() block.

# 4. use the RUNNING kernel's real config
sudo cp "$HDR/.config" "$SRC/.config"
( cd "$SRC" && sudo ./scripts/config --set-str SYSTEM_TRUSTED_KEYS '' \
                                     --set-str SYSTEM_REVOCATION_KEYS '' )
( cd "$SRC" && sudo make olddefconfig && sudo make -j$(nproc) modules_prepare )

# 5. pin version + symbols to the running kernel (no hacks)
sudo cp "$HDR/include/generated/utsrelease.h" "$SRC/include/generated/utsrelease.h"
sudo cp "$HDR/include/config/kernel.release"  "$SRC/include/config/kernel.release"
sudo cp "$HDR/Module.symvers"                 "$SRC/Module.symvers"

# 6. build (PATH must include $XE so the generated xe_gen_wa_oob tool is found)
( cd "$SRC" && sudo env PATH="$PATH:$XE" make -j$(nproc) M=drivers/gpu/drm/xe modules )

# 7. sanity check: vermagic must equal `uname -r`, WITHOUT having forced it
modinfo "$XE/xe.ko" | grep vermagic      # -> 7.0.0-27-generic (== uname -r)

# 8. install
sudo strip --strip-debug "$XE/xe.ko"
sudo zstd -f -19 "$XE/xe.ko" -o "$XE/xe.ko.zst"
MODDIR=/lib/modules/$KREL/kernel/drivers/gpu/drm/xe
sudo cp "$MODDIR/xe.ko.zst" /root/xe.ko.zst.stock-$KREL      # BACKUP first
sudo cp "$XE/xe.ko.zst" "$MODDIR/xe.ko.zst"
sudo depmod -a "$KREL"
sudo reboot                                                  # activate cleanly
```

---

## After reboot — verify it BOUND (don't skip this)

```bash
BDF=$(lspci -Dn | awk '/8086:e2(11|23)/{print $1; exit}')
lspci -k -s "$BDF" | grep 'in use'                       # -> Kernel driver in use: xe
ls /sys/class/drm/card*/device/hwmon/hwmon*/pwm1_enable  # fan sysfs
find /sys/devices -path '*gt0/oc/vf_curve'               # OC sysfs
```

If the display is **zoomed / low-res**, the module didn't bind. Restore stock and
reboot:

```bash
KREL=$(uname -r); MODDIR=/lib/modules/$KREL/kernel/drivers/gpu/drm/xe
sudo cp /root/xe.ko.zst.stock-$KREL "$MODDIR/xe.ko.zst"
sudo depmod -a "$KREL" && sudo reboot
```

## Enable persistence

The install copies the userland via `install.sh`; enable the boot services that
re-apply your saved fan curve + OC:

```bash
sudo systemctl enable --now xe-fan-curve.service xe-gpu-oc.service xe-gpu-vram.service
```

## ⚠️ Kernel updates

A kernel upgrade installs a **fresh stock `xe`** for the new kernel — you lose
fan/OC until you rebuild for it. Two options:

* Re-run `sudo bash scripts/build-xe-module.sh` after each kernel update, or
* Use the **DKMS** package (`dkms/`) so it **auto-rebuilds on every kernel
  update**. See [DISTRIBUTION.md](DISTRIBUTION.md).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `xe_trace.h: No such file` | Building against headers dir — build from the **full source tree** instead. |
| `dwarf.h: No such file` | Install `libdw-dev`. |
| `pahole: not found` / BTF errors | Install `dwarves` (BTF skip is otherwise harmless). |
| `xe_gen_wa_oob: not found` | Add `$XE` to `PATH` for the build. |
| ~1244 `undefined!` at modpost | `Module.symvers` empty — copy it from the headers package. |
| vermagic `7.0.6…` vs kernel `7.0.0…` | Copy `utsrelease.h`/`kernel.release` from headers; **don't** force it. |
| Loads (taints) but display is zoomed | ABI mismatch from an edited config / forced vermagic — rebuild per the two rules above. |
