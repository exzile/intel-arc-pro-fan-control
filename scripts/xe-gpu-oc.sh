#!/usr/bin/env bash
# xe-gpu-oc.sh - Intel Arc Pro B60/B70 (Battlemage) overclocking on Linux.
#
# Controls (via the xe_gt_oc patch sysfs under <device>/tile0/gt0/oc/):
#   * voltage-frequency curve  -> oc/vf_curve   (undervolt / overvolt)
#   * VRAM (GDDR6) memory speed -> oc/mem_speed  (Mbps)
#
# PERSISTENCE: the card resets to stock on every cold boot, so choices are saved
# to /etc/xe-gpu-oc.conf and re-applied at boot by xe-gpu-oc.service (`... boot`).
set -euo pipefail

GPU="${ARC_GPU_BDF:-0000:03:00.0}"
DEV="/sys/bus/pci/devices/$GPU"
OC="$DEV/tile0/gt0/oc/vf_curve"
MS="$DEV/tile0/gt0/oc/mem_speed"
NPTS=85
VMIN=400
VMAX=1200
MEM_MIN=14000
MEM_MAX=24000
MEM_STOCK=19000
STOCKDIR=/var/lib/xe-gpu-oc
STOCK="$STOCKDIR/stock-curve"
APPLIED="$STOCKDIR/applied-curve"
CONF=/etc/xe-gpu-oc.conf

die(){ echo "xe-gpu-oc: $*" >&2; exit 1; }
need_root(){ [ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"; }
have_oc(){ [ -e "$OC" ] || die "OC interface not found ($OC) - xe_gt_oc patch not loaded?"; }
have_mem(){ [ -e "$MS" ] || die "mem_speed interface not found ($MS) - update the xe_gt_oc patch"; }
wake(){ echo on > "$DEV/power/control" 2>/dev/null || true; }

read_curve(){ cat "$OC"; }

save_stock(){
  [ -f "$STOCK" ] && return 0
  mkdir -p "$STOCKDIR"; read_curve > "$STOCK"
  [ "$(wc -l < "$STOCK")" -eq "$NPTS" ] || { rm -f "$STOCK"; die "stock read incomplete"; }
  echo "saved stock curve -> $STOCK"
}

# update one key in the conf, preserving the others
persist_kv(){
  mkdir -p "$(dirname "$CONF")"; touch "$CONF"
  grep -vE "^$1=" "$CONF" 2>/dev/null > "$CONF.tmp" || true
  echo "$1=$2" >> "$CONF.tmp"; mv "$CONF.tmp" "$CONF"
}
conf_get(){ local k="$1"; [ -r "$CONF" ] && . "$CONF" 2>/dev/null || true; eval "echo \"\${$k:-}\""; }

apply_offset(){ awk -v o="$1" -v lo="$VMIN" -v hi="$VMAX" \
  '{ v=$2+o; if(v<lo)v=lo; if(v>hi)v=hi; printf "%d %d\n",$1,v }' "$STOCK" > "$OC"; }

cmd_read(){
  wake; have_oc
  printf "%-5s %s\n" "idx" "voltage(mV)"
  read_curve | while read -r idx mv; do printf "%-5s %s\n" "$idx" "$mv"; done
  if [ -e "$MS" ]; then echo; echo "memory speed: $(cat "$MS") Mbps"; fi
}

cmd_offset(){ # $1 ABSOLUTE mV offset from stock (idempotent), persisted
  need_root
  local off="$1"; [[ "$off" =~ ^-?[0-9]+$ ]] || die "offset must be integer mV"
  wake; have_oc; save_stock
  apply_offset "$off"; rm -f "$APPLIED"; persist_kv VOLTAGE_OFFSET "$off"
  echo "curve = stock ${off}mV (persisted). 'reset' to restore."
}

cmd_set(){ # $1 index $2 mV (per-point, custom mode)
  need_root
  local i="$1" mv="$2"
  [[ "$i" =~ ^[0-9]+$ ]] && (( i < NPTS )) || die "index 0..$((NPTS-1))"
  (( mv >= VMIN && mv <= VMAX )) || die "voltage must be ${VMIN}..${VMAX} mV"
  wake; have_oc; save_stock
  echo "$i $mv" > "$OC"; read_curve > "$APPLIED"; persist_kv VOLTAGE_OFFSET custom
  echo "set point #$i = ${mv}mV (persisted)."
}

cmd_mem(){ # $1 = memory speed in Mbps (e.g. 20000 = 20 Gbps), persisted
  need_root
  local mbps="$1"; [[ "$mbps" =~ ^[0-9]+$ ]] || die "memory speed must be Mbps (e.g. 20000)"
  (( mbps >= MEM_MIN && mbps <= MEM_MAX )) || die "memory speed must be ${MEM_MIN}..${MEM_MAX} Mbps"
  wake; have_mem
  echo "$mbps" > "$MS"; persist_kv MEM_SPEED "$mbps"
  echo "memory speed = ${mbps} Mbps ($(awk "BEGIN{printf \"%.2f\", $mbps/1000}") Gbps), persisted."
}

cmd_reset(){
  need_root; wake; have_oc
  [ -f "$STOCK" ] && cat "$STOCK" > "$OC"
  [ -e "$MS" ] && echo "$MEM_STOCK" > "$MS" || true
  rm -f "$APPLIED"; persist_kv VOLTAGE_OFFSET 0; persist_kv MEM_SPEED "$MEM_STOCK"
  echo "restored stock VF curve + memory speed (persisted state cleared)."
}

cmd_boot(){ # re-apply persisted choices (xe-gpu-oc.service)
  need_root
  [ -r "$CONF" ] || { echo "no $CONF; nothing to apply"; exit 0; }
  . "$CONF" 2>/dev/null || true
  wake
  local v="${VOLTAGE_OFFSET:-0}" m="${MEM_SPEED:-}"
  if [ -e "$OC" ]; then
    if [ "$v" = "custom" ] && [ -f "$APPLIED" ]; then
      cat "$APPLIED" > "$OC"; echo "boot: custom curve"
    elif [[ "$v" =~ ^-?[0-9]+$ ]] && [ "$v" != "0" ] && [ -f "$STOCK" ]; then
      apply_offset "$v"; echo "boot: ${v}mV offset"
    fi
  fi
  if [ -e "$MS" ] && [[ "$m" =~ ^[0-9]+$ ]] && [ "$m" != "$MEM_STOCK" ]; then
    echo "$m" > "$MS"; echo "boot: memory speed ${m} Mbps"
  fi
  echo "boot: done"
}

cmd_status(){
  echo "offset=$(conf_get VOLTAGE_OFFSET)"
  echo "mem_speed=$(conf_get MEM_SPEED)"
  [ -e "$MS" ] && echo "mem_speed_live=$(cat "$MS" 2>/dev/null)"
}

usage(){ cat <<EOF
xe-gpu-oc.sh - Arc Pro B60/B70 overclocking (Linux)

  read              show the VF curve (index -> mV) and memory speed
  offset <±mV>      set the curve to stock + offset (undervolt −, overvolt +)
  set <idx> <mV>    set a single VF-curve point
  mem <Mbps>        set VRAM (GDDR6) memory speed, e.g. 'mem 20000' = 20 Gbps
  reset             restore stock curve + memory speed
  boot              re-apply persisted choices (used by xe-gpu-oc.service)
  status            print persisted offset + memory speed

Choices are saved to $CONF and re-applied at boot. Voltage clamped
${VMIN}..${VMAX} mV; memory ${MEM_MIN}..${MEM_MAX} Mbps. Needs the xe_gt_oc patch. Writes need root.
EOF
}

case "${1:-}" in
  read)   cmd_read ;;
  offset) cmd_offset "${2:?offset mV required}" ;;
  set)    cmd_set "${2:?index}" "${3:?mV}" ;;
  mem)    cmd_mem "${2:?Mbps required}" ;;
  reset)  cmd_reset ;;
  boot)   cmd_boot ;;
  status) cmd_status ;;
  ""|-h|--help|help) usage ;;
  *) die "unknown command '$1' (try --help)" ;;
esac
