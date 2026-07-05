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

# locate the Arc (xe) GPU's PCI address: explicit override, else auto-detect, else legacy default
_detect_bdf(){
  local d drv
  for d in /sys/class/drm/card*/device; do
    drv=$(basename "$(readlink -f "$d/driver" 2>/dev/null)" 2>/dev/null)
    [ "$drv" = xe ] && { basename "$(readlink -f "$d")"; return 0; }
  done
  return 1
}
GPU="${ARC_GPU_BDF:-$(_detect_bdf || echo 0000:03:00.0)}"
DEV="/sys/bus/pci/devices/$GPU"
OC="$DEV/tile0/gt0/oc/vf_curve"
MS="$DEV/tile0/gt0/oc/mem_speed"
TL="$DEV/tile0/gt0/oc/temp_limit"
NPTS=85
VMIN=400
VMAX=1200
MEM_MIN=14000
MEM_MAX=24000
MEM_STOCK=19000
TEMP_MIN=60
TEMP_MAX=100
TEMP_STOCK=100
STOCKDIR=/var/lib/xe-gpu-oc
STOCK="$STOCKDIR/stock-curve"
APPLIED="$STOCKDIR/applied-curve"
CONF=/etc/xe-gpu-oc.conf
PROFDIR="$STOCKDIR/profiles"     # saved named OC profiles (voltage/memory/temp)

die(){ echo "xe-gpu-oc: $*" >&2; exit 1; }
need_root(){ [ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"; }
have_oc(){ [ -e "$OC" ] || die "OC interface not found ($OC) - xe_gt_oc patch not loaded?"; }
have_mem(){ [ -e "$MS" ] || die "mem_speed interface not found ($MS) - update the xe_gt_oc patch"; }
have_temp(){ [ -e "$TL" ] || die "temp_limit interface not found ($TL) - update the xe_gt_oc patch"; }
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

apply_offset(){ # $1 = offset mV, $2 = optional max mV ceiling (defaults to VMAX)
  local hi="${2:-$VMAX}"
  awk -v o="$1" -v lo="$VMIN" -v hi="$hi" \
    '{ v=$2+o; if(v<lo)v=lo; if(v>hi)v=hi; printf "%d %d\n",$1,v }' "$STOCK" > "$OC"; }

cmd_read(){
  wake; have_oc
  printf "%-5s %s\n" "idx" "voltage(mV)"
  read_curve | while read -r idx mv; do printf "%-5s %s\n" "$idx" "$mv"; done
  echo
  [ -e "$MS" ] && echo "memory speed: $(cat "$MS") Mbps"
  [ -e "$TL" ] && echo "temp limit:   $(cat "$TL") degC"
  true
}

cmd_offset(){ # $1 ABSOLUTE mV offset from stock (idempotent); $2 = optional max mV ceiling
  need_root
  local off="$1"; [[ "$off" =~ ^-?[0-9]+$ ]] || die "offset must be integer mV"
  local hi="${2:-$VMAX}"; [[ "$hi" =~ ^[0-9]+$ ]] || die "max must be integer mV"
  (( hi > VMIN && hi <= VMAX )) || die "max must be $((VMIN+1))..$VMAX mV"
  wake; have_oc; save_stock
  apply_offset "$off" "$hi"; rm -f "$APPLIED"
  persist_kv VOLTAGE_OFFSET "$off"; persist_kv VOLTAGE_MAX "$hi"
  echo "curve = stock ${off}mV (<= ${hi}mV, persisted). 'reset' to restore."
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

cmd_curve(){ # args: idx:mV idx:mV ...  -> full/partial custom VF curve in one transaction
  need_root
  [ "$#" -ge 1 ] || die "curve needs one or more idx:mV pairs"
  wake; have_oc; save_stock
  local out="" pair i mv
  for pair in "$@"; do
    i="${pair%%:*}"; mv="${pair##*:}"
    [[ "$i" =~ ^[0-9]+$ ]] && (( i < NPTS )) || die "index 0..$((NPTS-1)) ('$pair')"
    [[ "$mv" =~ ^[0-9]+$ ]] && (( mv >= VMIN && mv <= VMAX )) || die "mV ${VMIN}..${VMAX} ('$pair')"
    out+="$i $mv"$'\n'
  done
  printf '%s' "$out" > "$OC"                 # single begin -> write -> end transaction
  read_curve > "$APPLIED"; persist_kv VOLTAGE_OFFSET custom
  echo "applied custom VF curve ($# point(s), persisted)."
}

cmd_mem(){ # $1 = memory speed in Mbps (e.g. 20000 = 20 Gbps), persisted
  need_root
  local mbps="$1"; [[ "$mbps" =~ ^[0-9]+$ ]] || die "memory speed must be Mbps (e.g. 20000)"
  (( mbps >= MEM_MIN && mbps <= MEM_MAX )) || die "memory speed must be ${MEM_MIN}..${MEM_MAX} Mbps"
  wake; have_mem
  echo "$mbps" > "$MS"; persist_kv MEM_SPEED "$mbps"
  echo "memory speed = ${mbps} Mbps ($(awk "BEGIN{printf \"%.2f\", $mbps/1000}") Gbps), persisted."
}

cmd_temp(){ # $1 = temperature throttle limit in degrees C, persisted
  need_root
  local c="$1"; [[ "$c" =~ ^[0-9]+$ ]] || die "temperature limit must be an integer degC"
  (( c >= TEMP_MIN && c <= TEMP_MAX )) || die "temperature limit must be ${TEMP_MIN}..${TEMP_MAX} degC"
  wake; have_temp
  echo "$c" > "$TL"; persist_kv TEMP_LIMIT "$c"
  echo "temperature limit = ${c} degC, persisted."
}

cmd_reset(){
  need_root; wake; have_oc
  [ -f "$STOCK" ] && cat "$STOCK" > "$OC"
  [ -e "$MS" ] && echo "$MEM_STOCK" > "$MS" || true
  [ -e "$TL" ] && echo "$TEMP_STOCK" > "$TL" || true
  rm -f "$APPLIED"; persist_kv VOLTAGE_OFFSET 0; persist_kv VOLTAGE_MAX "$VMAX"
  persist_kv MEM_SPEED "$MEM_STOCK"; persist_kv TEMP_LIMIT "$TEMP_STOCK"
  echo "restored stock VF curve + memory speed + temp limit (persisted state cleared)."
}

cmd_boot(){ # re-apply persisted choices (xe-gpu-oc.service)
  need_root
  [ -r "$CONF" ] || { echo "no $CONF; nothing to apply"; exit 0; }
  . "$CONF" 2>/dev/null || true
  wake
  local v="${VOLTAGE_OFFSET:-0}" m="${MEM_SPEED:-}" t="${TEMP_LIMIT:-}" vmax="${VOLTAGE_MAX:-$VMAX}"
  if [ -e "$OC" ]; then
    if [ "$v" = "custom" ] && [ -f "$APPLIED" ]; then
      cat "$APPLIED" > "$OC"; echo "boot: custom curve"
    elif [[ "$v" =~ ^-?[0-9]+$ ]] && { [ "$v" != "0" ] || [ "$vmax" != "$VMAX" ]; } && [ -f "$STOCK" ]; then
      apply_offset "$v" "$vmax"; echo "boot: ${v}mV offset (<= ${vmax}mV)"
    fi
  fi
  if [ -e "$MS" ] && [[ "$m" =~ ^[0-9]+$ ]] && [ "$m" != "$MEM_STOCK" ]; then
    echo "$m" > "$MS"; echo "boot: memory speed ${m} Mbps"
  fi
  if [ -e "$TL" ] && [[ "$t" =~ ^[0-9]+$ ]] && [ "$t" != "$TEMP_STOCK" ]; then
    echo "$t" > "$TL"; echo "boot: temp limit ${t} degC"
  fi
  echo "boot: done"
}

prof_name_ok(){ [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die "profile name: letters, digits, . _ - only"; }

cmd_profile(){ # save|load|list|names|delete a named OC profile (voltage/memory/temp)
  local sub="${1:-}"; shift || true
  case "$sub" in
    list)
      [ -d "$PROFDIR" ] || { echo "(no saved profiles)"; return 0; }
      local f found=0 n
      for f in "$PROFDIR"/*.conf; do
        [ -e "$f" ] || continue; found=1; n=$(basename "$f" .conf)
        ( . "$f" 2>/dev/null
          printf "%-16s offset=%-7s max=%-5s mem=%-6s temp=%s\n" "$n" \
            "${VOLTAGE_OFFSET:-0}" "${VOLTAGE_MAX:-$VMAX}" "${MEM_SPEED:-$MEM_STOCK}" "${TEMP_LIMIT:-$TEMP_STOCK}" )
      done
      [ "$found" = 1 ] || echo "(no saved profiles)"
      ;;
    names)   # bare names, one per line (for the GUI)
      [ -d "$PROFDIR" ] || return 0
      local f; for f in "$PROFDIR"/*.conf; do [ -e "$f" ] && basename "$f" .conf; done
      ;;
    save)
      need_root; local name="${1:?profile name required}"; prof_name_ok "$name"
      [ -r "$CONF" ] || die "nothing applied yet to save (apply an OC first)"
      mkdir -p "$PROFDIR"
      local VOLTAGE_OFFSET VOLTAGE_MAX MEM_SPEED TEMP_LIMIT
      . "$CONF" 2>/dev/null || true
      { echo "VOLTAGE_OFFSET=${VOLTAGE_OFFSET:-0}"
        echo "VOLTAGE_MAX=${VOLTAGE_MAX:-$VMAX}"
        echo "MEM_SPEED=${MEM_SPEED:-$MEM_STOCK}"
        echo "TEMP_LIMIT=${TEMP_LIMIT:-$TEMP_STOCK}"; } > "$PROFDIR/$name.conf"
      if [ "${VOLTAGE_OFFSET:-0}" = custom ] && [ -f "$APPLIED" ]; then
        cp "$APPLIED" "$PROFDIR/$name.curve"        # preserve a per-point custom curve
      else
        rm -f "$PROFDIR/$name.curve"
      fi
      echo "saved profile '$name'."
      ;;
    load)
      need_root; local name="${1:?profile name required}"; prof_name_ok "$name"
      local pf="$PROFDIR/$name.conf"; [ -r "$pf" ] || die "no such profile '$name'"
      local VOLTAGE_OFFSET VOLTAGE_MAX MEM_SPEED TEMP_LIMIT
      . "$pf"
      wake; have_oc; save_stock
      if [ "${VOLTAGE_OFFSET:-0}" = custom ] && [ -f "$PROFDIR/$name.curve" ]; then
        cat "$PROFDIR/$name.curve" > "$OC"; read_curve > "$APPLIED"; persist_kv VOLTAGE_OFFSET custom
      else
        apply_offset "${VOLTAGE_OFFSET:-0}" "${VOLTAGE_MAX:-$VMAX}"; rm -f "$APPLIED"
        persist_kv VOLTAGE_OFFSET "${VOLTAGE_OFFSET:-0}"; persist_kv VOLTAGE_MAX "${VOLTAGE_MAX:-$VMAX}"
      fi
      if [ -e "$MS" ]; then echo "${MEM_SPEED:-$MEM_STOCK}" > "$MS"; persist_kv MEM_SPEED "${MEM_SPEED:-$MEM_STOCK}"; fi
      if [ -e "$TL" ]; then echo "${TEMP_LIMIT:-$TEMP_STOCK}" > "$TL"; persist_kv TEMP_LIMIT "${TEMP_LIMIT:-$TEMP_STOCK}"; fi
      echo "loaded profile '$name'."
      ;;
    delete|rm)
      need_root; local name="${1:?profile name required}"; prof_name_ok "$name"
      rm -f "$PROFDIR/$name.conf" "$PROFDIR/$name.curve"; echo "deleted profile '$name'."
      ;;
    *) die "profile: save|load|list|names|delete <name>" ;;
  esac
}

cmd_status(){
  echo "offset=$(conf_get VOLTAGE_OFFSET)"
  echo "mem_speed=$(conf_get MEM_SPEED)"
  echo "temp_limit=$(conf_get TEMP_LIMIT)"
  [ -e "$MS" ] && echo "mem_speed_live=$(cat "$MS" 2>/dev/null)"
  [ -e "$TL" ] && echo "temp_limit_live=$(cat "$TL" 2>/dev/null)"
  true
}

usage(){ cat <<EOF
xe-gpu-oc.sh - Arc Pro B60/B70 overclocking (Linux)

  read              show the VF curve (index -> mV) and memory speed
  offset <±mV> [max] set the curve to stock + offset (undervolt −, overvolt +);
                    optional max clamps the peak voltage (mV)
  set <idx> <mV>    set a single VF-curve point
  curve <idx:mV>... write a full/partial custom VF curve (one transaction)
  mem <Mbps>        set VRAM (GDDR6) memory speed, e.g. 'mem 20000' = 20 Gbps
  temp <degC>       set the GPU temperature (throttle) limit, e.g. 'temp 95'
  reset             restore stock curve + memory speed + temp limit
  profile save <n>  save the current OC (voltage/memory/temp) as a named profile
  profile load <n>  apply a saved profile
  profile list      list saved profiles; 'profile delete <n>' removes one
  boot              re-apply persisted choices (used by xe-gpu-oc.service)
  status            print persisted offset + memory speed + temp limit

Choices are saved to $CONF and re-applied at boot. Voltage clamped
${VMIN}..${VMAX} mV; memory ${MEM_MIN}..${MEM_MAX} Mbps. Needs the xe_gt_oc patch. Writes need root.
EOF
}

case "${1:-}" in
  read)   cmd_read ;;
  offset) cmd_offset "${2:?offset mV required}" "${3:-}" ;;
  set)    cmd_set "${2:?index}" "${3:?mV}" ;;
  curve)  shift; cmd_curve "$@" ;;
  mem)    cmd_mem "${2:?Mbps required}" ;;
  temp)   cmd_temp "${2:?degC required}" ;;
  reset)  cmd_reset ;;
  profile) shift; cmd_profile "$@" ;;
  boot)   cmd_boot ;;
  status) cmd_status ;;
  ""|-h|--help|help) usage ;;
  *) die "unknown command '$1' (try --help)" ;;
esac
