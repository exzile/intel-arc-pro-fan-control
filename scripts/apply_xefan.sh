#!/bin/bash
# Apply Intel fan-control patch series 168027 (adapted CachyOS 7.1.2 version) to the
# Ubuntu 7.0.0 xe driver, then rebuild ONLY the xe module.
#
# Prereq: the box is booted & healthy on the GENUINE STOCK xe.ko (fresh boot = stock).
# Run: bash apply_xefan.sh   (uses sudo; expects the patch files in the same dir)
set -e
SRC=/home/joey/linux-source-7.0.0
XE=$SRC/drivers/gpu/drm/xe
PATCH=xe-fan-control-168027-cachyos-7.1.2.patch
PW='UmFpbmluZzE='   # base64 sudo pw (per investigation notes)
s() { echo "$PW" | base64 -d | sudo -S sh -c "$1"; }

echo "=== STEP 0: confirm we're on stock and healthy ==="
uname -r
lsmod | grep '^xe ' || { echo "xe not loaded"; }
dmesg 2>/dev/null | grep -iE 'fault|CAT error|reset' | tail -3 || echo "  (no recent faults)"

echo "=== STEP 1: restore PRISTINE stock xe_hwmon.c + xe_pcode_api.h ==="
# Our working tree has exploratory edits; the patch needs pristine context.
# Re-extract just these two files from the untouched linux-source tarball.
if [ -f /usr/src/linux-source-7.0.0/linux-source-7.0.0.tar.bz2 ]; then
  TMP=$(mktemp -d)
  tar -xjf /usr/src/linux-source-7.0.0/linux-source-7.0.0.tar.bz2 \
      -C "$TMP" --strip-components=1 \
      linux-source-7.0.0/drivers/gpu/drm/xe/xe_hwmon.c \
      linux-source-7.0.0/drivers/gpu/drm/xe/xe_pcode_api.h 2>/dev/null || true
  if [ -f "$TMP/drivers/gpu/drm/xe/xe_hwmon.c" ]; then
    cp "$TMP/drivers/gpu/drm/xe/xe_hwmon.c" "$XE/xe_hwmon.c"
    cp "$TMP/drivers/gpu/drm/xe/xe_pcode_api.h" "$XE/xe_pcode_api.h"
    echo "  restored pristine xe_hwmon.c + xe_pcode_api.h from tarball"
  else
    echo "  !! could not extract from tarball; check paths. Aborting so we don't patch a modified file."
    exit 1
  fi
  rm -rf "$TMP"
else
  echo "  !! tarball not found at /usr/src/linux-source-7.0.0/. Provide a pristine xe_hwmon.c manually."
  exit 1
fi

echo "=== STEP 2: back up pristine, then dry-run the patch ==="
cp "$XE/xe_hwmon.c" "$XE/xe_hwmon.c.pristine"
cp "$XE/xe_pcode_api.h" "$XE/xe_pcode_api.h.pristine"
# Patch paths are cachyos-7.1.2-3/drivers/gpu/drm/xe/... -> strip 1 leading dir (-p1),
# run from $SRC so drivers/... and Documentation/... both resolve.
PATCHFILE="$(pwd)/$PATCH"
cd "$SRC"
echo "--- DRY RUN (no changes written) ---"
patch -p1 --dry-run --fuzz=5 -i "$PATCHFILE" 2>&1 | tail -50 || true
echo ""
echo ">>> Review the dry-run above. FAILED hunks in xe_hwmon.c / xe_pcode_api.h must be"
echo ">>> hand-ported using xe_hwmon.c.patched (the complete 7.1.2 target) as reference."
echo ">>> A FAILED hunk on the Documentation/ABI file is HARMLESS (docs, not built)."
echo ">>> If the two CODE files apply (fuzz/offset OK), re-run with:  APPLY=1 bash apply_xefan.sh"

if [ "${APPLY:-0}" = "1" ]; then
  echo "=== STEP 3: APPLYING for real ==="
  patch -p1 --fuzz=5 -i "$PATCHFILE" || echo "  (some hunks rejected — see *.rej; fix from xe_hwmon.c.patched)"
  echo "=== STEP 4: build the module ==="
  export PATH=$PATH:"$XE"
  make M=drivers/gpu/drm/xe modules 2>&1 | grep -iE 'error:|LD \[M\]  xe.ko' | head
  echo "  If xe.ko built: remove any stale xe.ko.zst first, then:"
  echo "    cp $XE/xe.ko /lib/modules/\$(uname -r)/kernel/drivers/gpu/drm/xe/xe.ko && depmod -a"
  echo "  Reload via PCI unbind (NOT rapid reloads): echo 0000:03:00.0 > /sys/bus/pci/drivers/xe/unbind; rmmod xe; modprobe xe; echo 0000:03:00.0 > .../bind"
fi
