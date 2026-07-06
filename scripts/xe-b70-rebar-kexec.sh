#!/bin/bash
# xe-b70-rebar-kexec - bind the Arc Pro B70 (Battlemage G31, 8086:e223) on an
# Above-4G-decoding-less platform (e.g. Intel Z370 / Coffee Lake).
#
# The B70 POSTs with a 32GB resizable VRAM BAR (plus a 32GB SR-IOV VF BAR). On a
# board without Above-4G Decoding / large-enough MMIO, that BAR cannot be mapped
# below 4GB, so the whole bus bridge window collapses and `xe` fails to probe the
# card ("failed to map registers", -EIO). Enabling Above-4G on such a board tends
# to starve other devices (e.g. an NVMe boot drive) of MMIO instead.
#
# The B60 (G21) never hit this because it POSTs a small 256MB BAR. This script
# makes the B70 behave the same way: it shrinks the B70's physical VRAM BAR to
# 256MB via the PCIe Resizable BAR control register, then kexecs into the same
# kernel. kexec skips the PCIe reset, so the 256MB setting survives and the fresh
# enumeration maps the card cleanly below 4GB. Small-BAR mode is fine for fan
# control / telemetry / power+clock tuning (none of which touch VRAM directly).
#
# One-shot and loop-guarded via a kernel-cmdline flag: it can never kexec twice
# in a chain. See docs/B70-G31-MULTI-GPU.md.
set -uo pipefail

FLAG="xe_b70_kexeced"
CTRL=0x428                      # RBAR physical control reg for BAR2 on the B70
LOG=/var/log/xe-b70-rebar.log
exec >>"$LOG" 2>&1
echo "=== $(date -u) xe-b70-rebar start (cmdline: $(cat /proc/cmdline)) ==="

# Already arrived via our kexec? Never kexec again in this chain (loop guard).
if grep -qw "${FLAG}=1" /proc/cmdline; then
  echo "post-kexec boot; leaving PCI as-is."
  exit 0
fi

# Locate the B70 (Battlemage G31) by device id; allow an override via XE_B70_BDF.
DEV="${XE_B70_BDF:-}"
if [ -z "$DEV" ]; then
  for d in /sys/bus/pci/devices/*; do
    [ "$(cat "$d/device" 2>/dev/null)" = "0xe223" ] || continue
    DEV=$(basename "$d"); break
  done
fi
[ -n "$DEV" ] || { echo "no B70 (8086:e223) present; nothing to do."; exit 0; }
SYS=/sys/bus/pci/devices/$DEV
echo "B70 at $DEV"

# Already bound? (platform maps it, or Above-4G is enabled) -> nothing to do.
if [ -e "$SYS/driver" ]; then
  echo "B70 already bound to $(basename "$(readlink -f "$SYS/driver")"); no shrink needed."
  exit 0
fi

cur=$(setpci -s "$DEV" ${CTRL}.L 2>/dev/null) || { echo "setpci read failed"; exit 1; }
echo "RBAR ctrl ($CTRL) = $cur"
idx=$(( 0x$cur & 0x7 ))
if [ "$idx" != "2" ]; then
  echo "ERROR: RBAR ctrl at $CTRL controls BAR $idx not 2; refusing to guess."; exit 1
fi

# size field bits[13:8] = 8  =>  2^8 MB = 256MB, preserving all other bits
val=$(( (0x$cur & ~0x3f00) | (8 << 8) ))
printf 'writing RBAR ctrl %s = 0x%08x (256MB)\n' "$CTRL" "$val"
setpci -s "$DEV" ${CTRL}.L=$(printf '0x%08x' "$val") || { echo "setpci write failed"; exit 1; }

newcur=$(setpci -s "$DEV" ${CTRL}.L)
sz=$(( (0x$newcur >> 8) & 0x3f ))
echo "RBAR ctrl now = $newcur (size field=$sz)"
[ "$sz" = "8" ] || { echo "ERROR: size field $sz != 8 after write; aborting kexec."; exit 1; }

command -v kexec >/dev/null 2>&1 || { echo "ERROR: kexec not installed; cannot rebind."; exit 1; }
KVER=$(uname -r)
KERNEL=/boot/vmlinuz-$KVER
INITRD=/boot/initrd.img-$KVER
[ -r "$KERNEL" ] && [ -r "$INITRD" ] || { echo "ERROR: missing $KERNEL / $INITRD"; exit 1; }
CMDLINE="$(cat /proc/cmdline) ${FLAG}=1"

echo "kexec -l $KERNEL (append ${FLAG}=1)"
kexec -l "$KERNEL" --initrd="$INITRD" --command-line="$CMDLINE" || { echo "kexec -l failed"; exit 1; }
echo "syncing + systemctl kexec"
sync
systemctl kexec
