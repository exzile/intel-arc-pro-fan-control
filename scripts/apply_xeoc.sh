#!/usr/bin/env bash
#
# DEPRECATED: prefer scripts/build-xe-module.sh (does fan + OC build+install with
# the correct config/vermagic so the module binds). See docs/LINUX-BUILD.md.
#
# apply_xeoc.sh - add the voltage-frequency-curve overclocking sysfs (xe_gt_oc)
# to the in-tree xe driver and rebuild ONLY the xe module.
#
# Adds:  <device>/tile0/gt0/oc/vf_curve   (see docs/OVERCLOCKING.md)
#
# This composes with the fan-control build (apply_xefan.sh): run the fan apply
# first if you want both, then this. It only ADDS a new source file plus two
# one-line wires (Makefile, xe_gt.c), so it never conflicts with the fan patch.
#
# Prereq: kernel source for the running kernel under /home/<you>/linux-source-<base>
# or /usr/src/linux-source-<base>, and the matching headers/build dir.
# Usage:  sudo bash apply_xeoc.sh
set -euo pipefail

KREL="$(uname -r)"
BASE="$(echo "$KREL" | sed -E 's/-[0-9]+-.*$//')"
HERE="$(cd "$(dirname "$0")/.." && pwd)"          # repo root
KDIR="$HERE/kernel"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -f "$KDIR/xe_gt_oc.c" ] || { echo "missing $KDIR/xe_gt_oc.c"; exit 1; }

SRC=""
for c in "/home/$SUDO_USER/linux-source-$BASE" "/usr/src/linux-source-$BASE" \
         /home/*/linux-source-"$BASE"; do
  [ -d "$c/drivers/gpu/drm/xe" ] && { SRC="$c"; break; }
done
[ -n "$SRC" ] || { echo "no kernel source for base $BASE (apt-get install linux-source-$BASE)"; exit 1; }
XE="$SRC/drivers/gpu/drm/xe"
HDR="/lib/modules/$KREL/build"
[ -d "$HDR" ] || { echo "no headers/build dir at $HDR"; exit 1; }
echo "source: $SRC"

echo "=== 1. install OC source ==="
cp "$KDIR/xe_gt_oc.c" "$KDIR/xe_gt_oc.h" "$XE/"

echo "=== 2. wire Makefile + xe_gt.c (idempotent) ==="
grep -q 'xe_gt_oc.o' "$XE/Makefile" || \
  sed -i 's|\txe_gt_freq.o \\|\txe_gt_freq.o \\\n\txe_gt_oc.o \\|' "$XE/Makefile"
grep -q '#include "xe_gt_oc.h"' "$XE/xe_gt.c" || \
  sed -i 's|#include "xe_gt_freq.h"|#include "xe_gt_freq.h"\n#include "xe_gt_oc.h"|' "$XE/xe_gt.c"
grep -q 'xe_gt_oc_init' "$XE/xe_gt.c" || \
  perl -0777 -pi -e 's/(\terr = xe_gt_freq_init\(gt\);\n\tif \(err\)\n\t\treturn err;\n)/$1\n\terr = xe_gt_oc_init(gt);\n\tif (err)\n\t\treturn err;\n/' "$XE/xe_gt.c"

echo "=== 3. prepare + build (from source root so xe_gen_wa_oob host tool builds) ==="
[ -f "/boot/config-$KREL" ] && cp "/boot/config-$KREL" "$SRC/.config"
[ -f "$HDR/Module.symvers" ] && cp "$HDR/Module.symvers" "$SRC/Module.symvers"
( cd "$SRC" && make olddefconfig >/dev/null 2>&1 || true )
( cd "$SRC" && make modules_prepare >/dev/null 2>&1 || true )
export PATH="$PATH:$XE"
( cd "$SRC" && make -j"$(nproc)" M=drivers/gpu/drm/xe modules ) >/tmp/xe-oc-build.log 2>&1 || {
  echo "build failed — see /tmp/xe-oc-build.log"; tail -8 /tmp/xe-oc-build.log; exit 1; }
[ -f "$XE/xe.ko" ] || { echo "no xe.ko produced"; exit 1; }
[ "$(strings "$XE/xe.ko" | grep -c vf_curve)" -gt 0 ] || { echo "built xe.ko lacks vf_curve — aborting"; exit 1; }

echo "=== 4. install module ==="
MODDIR="/lib/modules/$KREL/kernel/drivers/gpu/drm/xe"
[ -f "$MODDIR/xe.ko.zst" ] && mv -f "$MODDIR/xe.ko.zst" "$MODDIR/xe.ko.zst.pre-oc"
[ -f "$MODDIR/xe.ko" ] && cp -f "$MODDIR/xe.ko" "$MODDIR/xe.ko.pre-oc"
cp "$XE/xe.ko" "$MODDIR/xe.ko"
depmod -a "$KREL"
echo "SUCCESS: OC-enabled xe.ko installed for $KREL."
echo "Reboot (xe is in use), then:  sudo xe-gpu oc read"
