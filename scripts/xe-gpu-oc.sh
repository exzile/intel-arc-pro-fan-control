#!/usr/bin/env bash
# xe-gpu-oc.sh - Intel Arc Pro B60/B70 (Battlemage) overclocking on Linux.
#
# Tunes the GPU voltage-frequency (VF) curve through the xe driver's
#   <device>/tile0/gt0/oc/vf_curve
# sysfs attribute (added by the xe_gt_oc patch - see kernel/README). The
# attribute performs the PCODE "late-binding" write transaction that the stock
# driver omits; each of the 85 points is a frequency step whose value is the
# voltage (mV).
#
#   read format:  "<index> <voltage_mV>" per line
#   write format: one or more "<index> <voltage_mV>" lines (partial ok)
set -euo pipefail

GPU="${ARC_GPU_BDF:-0000:03:00.0}"
DEV="/sys/bus/pci/devices/$GPU"
OC="$DEV/tile0/gt0/oc/vf_curve"
NPTS=85
VMIN=400
VMAX=1200
STOCKDIR=/var/lib/xe-gpu-oc
STOCK="$STOCKDIR/stock-curve"

die(){ echo "xe-gpu-oc: $*" >&2; exit 1; }
need_root(){ [ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"; }
have_oc(){ [ -e "$OC" ] || die "OC interface not found at $OC (xe_gt_oc patch not loaded?)"; }
wake(){ echo on > "$DEV/power/control" 2>/dev/null || true; sleep 0.5; }   # PCODE needs the GPU awake

read_curve(){ cat "$OC"; }   # -> "idx mV" lines

save_stock(){
  [ -f "$STOCK" ] && return 0
  mkdir -p "$STOCKDIR"; read_curve > "$STOCK"
  [ "$(wc -l < "$STOCK")" -eq "$NPTS" ] || { rm -f "$STOCK"; die "stock read incomplete"; }
  echo "saved stock curve -> $STOCK"
}

cmd_read(){
  wake; have_oc
  printf "%-5s %s\n" "idx" "voltage(mV)"
  read_curve | while read -r idx mv; do printf "%-5s %s\n" "$idx" "$mv"; done
}

cmd_offset(){ # $1 signed mV applied to every point
  local off="$1"; [[ "$off" =~ ^-?[0-9]+$ ]] || die "offset must be integer mV (e.g. -25 or 30)"
  wake; have_oc; save_stock
  read_curve | awk -v o="$off" -v lo="$VMIN" -v hi="$VMAX" \
    '{ v=$2+o; if(v<lo)v=lo; if(v>hi)v=hi; printf "%d %d\n",$1,v }' > "$OC"
  echo "applied ${off}mV to all $NPTS points (clamped ${VMIN}..${VMAX}). 'reset' to restore."
}

cmd_set(){ # $1 index $2 mV
  local i="$1" mv="$2"
  [[ "$i" =~ ^[0-9]+$ ]] && (( i < NPTS )) || die "index 0..$((NPTS-1))"
  (( mv >= VMIN && mv <= VMAX )) || die "voltage must be ${VMIN}..${VMAX} mV"
  wake; have_oc; save_stock
  echo "$i $mv" > "$OC"
  echo "set point #$i = ${mv}mV."
}

cmd_reset(){
  [ -f "$STOCK" ] || die "no saved stock curve ($STOCK) - nothing applied yet"
  wake; have_oc
  cat "$STOCK" > "$OC"
  echo "restored stock VF curve."
}

usage(){ cat <<EOF
xe-gpu-oc.sh - Arc Pro B60/B70 voltage-frequency curve OC (Linux)

  read              show the 85-point VF curve (index -> voltage mV)
  offset <±mV>      shift every point (offset -25 = undervolt, offset 30 = overvolt)
  set <idx> <mV>    set a single point
  reset             restore the saved stock curve

Voltage clamped ${VMIN}..${VMAX} mV; first change saves stock to $STOCK.
Requires the xe_gt_oc kernel patch (sysfs: $OC). Root required.
Pair with power/clock limits from xe-gpu-tune.sh.
EOF
}

need_root
case "${1:-}" in
  read)   cmd_read ;;
  offset) cmd_offset "${2:?offset mV required}" ;;
  set)    cmd_set "${2:?index}" "${3:?mV}" ;;
  reset)  cmd_reset ;;
  ""|-h|--help|help) usage ;;
  *) die "unknown command '$1' (try --help)" ;;
esac
