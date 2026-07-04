#!/bin/bash
# xe-fan-rebuild — rebuild & install the patched (fan-control) xe.ko for a kernel.
# Not true DKMS (xe can't build out-of-tree against headers — it needs the full
# kernel source with i915 siblings), so this drives the full-source build that works.
#
# Usage: sudo xe-fan-rebuild [KERNEL_RELEASE]   (default: running kernel)
# Idempotent: if that kernel's xe.ko already exposes fan control, it does nothing.
# Fails SAFE: on any error it leaves the existing module in place and logs loudly.
set -uo pipefail

KREL="${1:-$(uname -r)}"
PATCH=/usr/local/share/xe-fan/xe-fan-control-168027-cachyos-7.1.2.patch
LOG="logger -t xe-fan-rebuild -s"
$LOG "starting rebuild for kernel $KREL"

MODDIR="/lib/modules/$KREL/kernel/drivers/gpu/drm/xe"
[ -f "$PATCH" ] || { $LOG "ERROR: patch missing at $PATCH — cannot rebuild"; exit 1; }

# --- already patched? (grep the installed module for a fan-control sysfs string) ---
# Use grep -c (reads all input) not -q, so 'set -o pipefail' doesn't flag the
# SIGPIPE that -q would cause on the large 'strings' output.
if [ -f "$MODDIR/xe.ko" ]; then
  _fc=$(strings "$MODDIR/xe.ko" 2>/dev/null | grep -cE 'pwm1_auto_point[0-9]' || true)
  if [ "${_fc:-0}" -gt 0 ]; then
    $LOG "xe.ko for $KREL already has fan control — nothing to do"; exit 0
  fi
fi

# --- locate a matching full source tree ---
# base version = strip the -NN-flavour  (e.g. 7.0.0-28-generic -> 7.0.0)
BASE=$(echo "$KREL" | sed -E 's/-[0-9]+-.*$//')
SRC=""
for cand in "/home/joey/linux-source-$BASE" "/usr/src/linux-source-$BASE"; do
  [ -d "$cand/drivers/gpu/drm/xe" ] && { SRC="$cand"; break; }
done
if [ -z "$SRC" ]; then
  # try to extract from the tarball if present
  TB="/usr/src/linux-source-$BASE.tar.bz2"
  if [ -f "$TB" ]; then
    $LOG "extracting source from $TB ..."
    ( cd /usr/src && tar -xjf "$TB" ) && SRC="/usr/src/linux-source-$BASE"
  fi
fi
[ -n "$SRC" ] && [ -d "$SRC/drivers/gpu/drm/xe" ] || {
  $LOG "ERROR: no kernel source for base $BASE. Install it:  apt-get install linux-source-$BASE  — fan control NOT rebuilt for $KREL"
  exit 1
}
$LOG "using source tree $SRC"

XE="$SRC/drivers/gpu/drm/xe"
HDR="/lib/modules/$KREL/build"
[ -d "$HDR" ] || { $LOG "ERROR: no headers/build dir for $KREL ($HDR)"; exit 1; }

# --- prepare the source tree to build for THIS kernel ---
# config + symbol CRCs must match the target kernel
if [ -f "/boot/config-$KREL" ]; then cp "/boot/config-$KREL" "$SRC/.config"; fi
if [ -f "$HDR/Module.symvers" ]; then cp "$HDR/Module.symvers" "$SRC/Module.symvers"; fi
( cd "$SRC" && make olddefconfig >/dev/null 2>&1 || true )
( cd "$SRC" && make modules_prepare >/dev/null 2>&1 || true )

# --- restore pristine xe_hwmon.c/xe_pcode_api.h, then apply the patch ---
TMP=$(mktemp -d)
TB="/usr/src/linux-source-$BASE.tar.bz2"
if [ -f "$TB" ]; then
  tar -xjf "$TB" -C "$TMP" --strip-components=1 \
    "linux-source-$BASE/drivers/gpu/drm/xe/xe_hwmon.c" \
    "linux-source-$BASE/drivers/gpu/drm/xe/xe_pcode_api.h" 2>/dev/null && {
      cp "$TMP/drivers/gpu/drm/xe/xe_hwmon.c" "$XE/xe_hwmon.c"
      cp "$TMP/drivers/gpu/drm/xe/xe_pcode_api.h" "$XE/xe_pcode_api.h"
    }
fi
rm -rf "$TMP"
( cd "$SRC" && patch -p1 --forward --fuzz=5 -i "$PATCH" ) 2>&1 | $LOG || {
  # --forward makes an already-applied patch a no-op success; a real reject is an error
  if ! ( cd "$SRC" && patch -p1 --dry-run --reverse --fuzz=5 -i "$PATCH" >/dev/null 2>&1 ); then
    $LOG "ERROR: patch failed to apply to $BASE source (needs manual re-fuzz for this kernel) — fan control NOT rebuilt"
    exit 1
  fi
}

# --- build the module ---
export PATH="$PATH:$XE"
$LOG "building xe.ko for $KREL ..."
if ! ( cd "$SRC" && make -j"$(nproc)" M=drivers/gpu/drm/xe modules ) >/tmp/xe-fan-build.log 2>&1; then
  $LOG "ERROR: build failed — see /tmp/xe-fan-build.log — fan control NOT rebuilt for $KREL"
  tail -5 /tmp/xe-fan-build.log | $LOG
  exit 1
fi
[ -f "$XE/xe.ko" ] || { $LOG "ERROR: build produced no xe.ko"; exit 1; }

# --- install (remove stale .zst which modprobe prefers) ---
mkdir -p "$MODDIR"
[ -f "$MODDIR/xe.ko.zst" ] && mv "$MODDIR/xe.ko.zst" "$MODDIR/xe.ko.zst.stock-bak"
cp "$XE/xe.ko" "$MODDIR/xe.ko"
depmod -a "$KREL"
$LOG "SUCCESS: installed fan-control xe.ko for $KREL (reboot or reload xe to activate)"
exit 0
