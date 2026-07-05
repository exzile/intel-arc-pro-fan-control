#!/usr/bin/env bash
# xe-gpu-oc.sh - Intel Arc Pro B60/B70 (Battlemage) overclocking on Linux.
#
# Tunes the GPU voltage-frequency (VF) curve through the xe driver's
#   <device>/tile0/gt0/oc/vf_curve
# sysfs attribute (added by the xe_gt_oc patch). Each of the 85 points is a
# frequency step whose value is the voltage (mV).
#
# PERSISTENCE: the card resets the curve to stock on every cold boot, so the
# chosen offset is saved to /etc/xe-gpu-oc.conf and re-applied at boot by
# xe-gpu-oc.service (`xe-gpu-oc boot`). 'offset' is absolute-from-stock, so it is
# idempotent and safe to re-apply.
set -euo pipefail

GPU="${ARC_GPU_BDF:-0000:03:00.0}"
DEV="/sys/bus/pci/devices/$GPU"
OC="$DEV/tile0/gt0/oc/vf_curve"
NPTS=85
VMIN=400
VMAX=1200
STOCKDIR=/var/lib/xe-gpu-oc
STOCK="$STOCKDIR/stock-curve"        # true stock curve, saved once ("idx mV" lines)
APPLIED="$STOCKDIR/applied-curve"    # full curve for per-point 'set' (custom mode)
CONF=/etc/xe-gpu-oc.conf             # persisted choice (VOLTAGE_OFFSET=<mV|custom>)

die(){ echo "xe-gpu-oc: $*" >&2; exit 1; }
need_root(){ [ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"; }
have_oc(){ [ -e "$OC" ] || die "OC interface not found at $OC (xe_gt_oc patch not loaded?)"; }
wake(){ echo on > "$DEV/power/control" 2>/dev/null || true; }  # kernel also force-wakes for us

read_curve(){ cat "$OC"; }   # -> "idx mV" lines (unprivileged; kernel force-wakes)

save_stock(){                # capture TRUE stock once (call while at offset 0)
  [ -f "$STOCK" ] && return 0
  mkdir -p "$STOCKDIR"; read_curve > "$STOCK"
  [ "$(wc -l < "$STOCK")" -eq "$NPTS" ] || { rm -f "$STOCK"; die "stock read incomplete"; }
  echo "saved stock curve -> $STOCK"
}

persist(){ mkdir -p "$(dirname "$CONF")"; printf '# xe-gpu-oc persisted state (re-applied at boot by xe-gpu-oc.service)\nVOLTAGE_OFFSET=%s\n' "$1" > "$CONF"; }
conf_offset(){ [ -r "$CONF" ] && . "$CONF" 2>/dev/null; echo "${VOLTAGE_OFFSET:-0}"; }

apply_offset(){ # $1 absolute mV from stock -> write stock+off (clamped)
  awk -v o="$1" -v lo="$VMIN" -v hi="$VMAX" \
    '{ v=$2+o; if(v<lo)v=lo; if(v>hi)v=hi; printf "%d %d\n",$1,v }' "$STOCK" > "$OC"
}

cmd_read(){
  wake; have_oc
  printf "%-5s %s\n" "idx" "voltage(mV)"
  read_curve | while read -r idx mv; do printf "%-5s %s\n" "$idx" "$mv"; done
}

cmd_offset(){ # $1 = ABSOLUTE mV offset from stock (idempotent). persisted.
  need_root
  local off="$1"; [[ "$off" =~ ^-?[0-9]+$ ]] || die "offset must be integer mV (e.g. -25 or 30)"
  wake; have_oc; save_stock
  apply_offset "$off"
  rm -f "$APPLIED"            # offset mode, not custom
  persist "$off"
  echo "set curve to stock ${off:+with }${off}mV offset (persisted -> reapplied at boot). 'reset' to restore."
}

cmd_set(){ # $1 index $2 mV  -- per-point (custom mode); persisted as a full curve
  need_root
  local i="$1" mv="$2"
  [[ "$i" =~ ^[0-9]+$ ]] && (( i < NPTS )) || die "index 0..$((NPTS-1))"
  (( mv >= VMIN && mv <= VMAX )) || die "voltage must be ${VMIN}..${VMAX} mV"
  wake; have_oc; save_stock
  echo "$i $mv" > "$OC"
  read_curve > "$APPLIED"    # snapshot the resulting curve for boot re-apply
  persist "custom"
  echo "set point #$i = ${mv}mV (persisted)."
}

cmd_reset(){
  need_root; wake; have_oc
  [ -f "$STOCK" ] && cat "$STOCK" > "$OC"
  rm -f "$APPLIED"; persist 0
  echo "restored stock VF curve (persisted offset cleared)."
}

cmd_boot(){  # re-apply the persisted choice at boot (used by xe-gpu-oc.service)
  need_root
  [ -r "$CONF" ] || { echo "no $CONF; nothing to apply"; exit 0; }
  . "$CONF" 2>/dev/null || true
  local v="${VOLTAGE_OFFSET:-0}"
  [ -e "$OC" ] || { echo "OC sysfs not present; xe_gt_oc patch not loaded"; exit 0; }
  wake
  if [ "$v" = "custom" ] && [ -f "$APPLIED" ]; then
    cat "$APPLIED" > "$OC"; echo "boot: re-applied custom curve"
  elif [[ "$v" =~ ^-?[0-9]+$ ]] && [ "$v" != "0" ]; then
    [ -f "$STOCK" ] || { echo "boot: no stock baseline; skip"; exit 0; }
    apply_offset "$v"; echo "boot: re-applied ${v}mV offset"
  else
    echo "boot: nothing persisted"
  fi
}

cmd_status(){ echo "offset=$(conf_offset)"; }   # for the GUI / scripts (no root needed)

usage(){ cat <<EOF
xe-gpu-oc.sh - Arc Pro B60/B70 voltage-frequency curve OC (Linux)

  read              show the 85-point VF curve (index -> voltage mV)
  offset <±mV>      set the curve to stock + offset (absolute; undervolt −, overvolt +)
  set <idx> <mV>    set a single point
  reset             restore the stock curve
  boot              re-apply the persisted choice (used by xe-gpu-oc.service)
  status            print the persisted offset (offset=<mV|custom>)

The chosen offset is saved to $CONF and re-applied at boot (the card forgets on
cold boot). Voltage clamped ${VMIN}..${VMAX} mV; stock saved to $STOCK.
Requires the xe_gt_oc kernel patch (sysfs: $OC). Writes need root.
Pair with power/clock limits from xe-gpu-tune.sh.
EOF
}

case "${1:-}" in
  read)   cmd_read ;;
  offset) cmd_offset "${2:?offset mV required}" ;;
  set)    cmd_set "${2:?index}" "${3:?mV}" ;;
  reset)  cmd_reset ;;
  boot)   cmd_boot ;;
  status) cmd_status ;;
  ""|-h|--help|help) usage ;;
  *) die "unknown command '$1' (try --help)" ;;
esac
