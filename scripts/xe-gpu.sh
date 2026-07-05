#!/bin/bash
# xe-gpu — one-stop status dashboard + front-end for the Intel Arc (xe) GPU toolkit.
# Combines fan, clocks, power and all temperatures in a single view, and delegates
# control subcommands to the individual helpers.
#
#   xe-gpu               # full status dashboard (fan + clocks + power + temps)
#   xe-gpu watch [secs]  # live dashboard (default 2s)
#   xe-gpu fan   ...      # -> xe-fan-curve  (show|set|auto|max|boot)
#   xe-gpu tune  ...      # -> xe-gpu-tune   (show|set|reset|boot)
#   xe-gpu temps ...      # -> xe-gpu-temps  (show|watch|json)
set -uo pipefail

# --- locate the xe GPU card + matching hwmon (don't hardcode cardN/hwmonN) ---
CARD=""
for c in /sys/class/drm/card*; do
  [ -e "$c/device/driver" ] || continue
  [ "$(basename "$(readlink -f "$c/device/driver")")" = "xe" ] && { CARD="$c"; break; }
done
HW=""
for d in /sys/class/hwmon/hwmon*; do
  [ -r "$d/name" ] && [ "$(cat "$d/name")" = "xe" ] && { HW="$d"; break; }
done

rd() { cat "$1" 2>/dev/null; }
mC() { local v; v=$(rd "$1"); [ -n "$v" ] && echo $(( v / 1000 )) || echo ""; }

delegate() { # $1 = helper name, rest = args
  local h="$1"; shift
  if command -v "$h" >/dev/null 2>&1; then exec "$h" "$@"; fi
  for p in /usr/local/bin/"$h" "$(dirname "$0")/${h#xe-}.sh" "$(dirname "$0")/$h.sh"; do
    [ -x "$p" ] && exec "$p" "$@"
  done
  echo "error: helper '$h' not found (install it from scripts/)" >&2; exit 1
}

hottest() {
  local hot=-1 lbl=""
  for f in "$HW"/temp*_input; do
    local n t l; n=$(basename "$f" _input | sed 's/temp//')
    t=$(mC "$f"); [ -z "$t" ] && continue
    if [ "$t" -gt "$hot" ] 2>/dev/null; then hot=$t; l=$(rd "$HW/temp${n}_label"); lbl=${l:-temp$n}; fi
  done
  echo "$lbl $hot"
}

status() {
  local W=64 rule
  rule=$(printf '─%.0s' $(seq 1 $W))
  printf '── Intel Arc (xe) GPU ── %s %s\n' "$(date '+%H:%M:%S')" "${rule:0:$((W-25))}"
  # identity
  if [ -n "$CARD" ]; then
    local pci did
    pci=$(basename "$(readlink -f "$CARD/device")" 2>/dev/null)
    did=$(rd "$CARD/device/device")
    printf "  card   : %s   pci %s   id 8086:%s\n" "$(basename "$CARD")" "${pci:-?}" "${did#0x}"
  else
    echo "  card   : (no xe DRM card found)"
  fi

  # clocks
  local GT="$CARD/device/tile0/gt0/freq0"
  if [ -d "$GT" ]; then
    printf "  clocks : cur %s / min %s / max %s MHz  (hw %s..%s, eff %s)\n" \
      "$(rd $GT/cur_freq)" "$(rd $GT/min_freq)" "$(rd $GT/max_freq)" \
      "$(rd $GT/rpn_freq)" "$(rd $GT/rp0_freq)" "$(rd $GT/rpe_freq)"
  fi

  # power
  if [ -n "$HW" ]; then
    local pcap pcrit
    pcap=$(rd "$HW/power1_cap"); pcrit=$(rd "$HW/power1_crit")
    { [ -n "$pcap" ] && [ "$pcap" != 0 ]; } && printf "  power  : cap %s W" "$(( pcap/1000000 ))" \
      || printf "  power  : cap (unset)"
    [ -n "$pcrit" ] && printf "   I1 crit %.2f W" "$(awk "BEGIN{print $pcrit/1000000}")"
    echo
  fi

  # fan
  if [ -n "$HW" ]; then
    local fan fmax pwm mode
    fan=$(rd "$HW/fan1_input"); fmax=$(rd "$HW/fan1_max"); pwm=$(rd "$HW/pwm1_enable")
    case "${pwm:-}" in 0) mode="full-speed";; 1) mode="manual curve";; 2) mode="auto-stock";; *) mode="?";; esac
    printf "  fan    : %s rpm" "${fan:-?}"
    [ -n "$fmax" ] && printf " / %s max" "$fmax"
    printf "   (mode: %s)\n" "$mode"
  fi

  # temps: key channels + hottest
  if [ -n "$HW" ]; then
    echo "  temps  :"
    for want in pkg mctrl pcie vram; do
      for f in "$HW"/temp*_label; do
        [ "$(rd "$f")" = "$want" ] || continue
        local n t crit; n=$(basename "$f" _label | sed 's/temp//')
        t=$(mC "$HW/temp${n}_input"); crit=$(mC "$HW/temp${n}_crit")
        printf "           %-6s %3s°C  (crit %s)\n" "$want" "${t:-?}" "${crit:-–}"
      done
    done
    local h; h=$(hottest)
    printf "           %-6s %s°C  <- hottest sensor\n" "hot:" "${h#* }"
    echo "           (all 18 sensors: xe-gpu temps)"
  fi
  printf '%s\n' "$rule"
}

case "${1:-status}" in
  status|show|"") status ;;
  watch)
    secs="${2:-2}"; trap 'echo; exit 0' INT
    while true; do clear 2>/dev/null; status; echo "  (watch ${secs}s — Ctrl-C to stop)"; sleep "$secs"; done ;;
  fan)   shift; delegate xe-fan-curve "$@" ;;
  tune)  shift; delegate xe-gpu-tune  "$@" ;;
  temps) shift; delegate xe-gpu-temps "$@" ;;
  oc)    shift; delegate xe-gpu-oc    "$@" ;;
  -h|--help|help)
    echo "usage: xe-gpu {status|watch [secs]|fan ...|tune ...|temps ...|oc ...}"
    echo "  status        full dashboard (fan+clocks+power+temps)"
    echo "  watch [secs]  live dashboard"
    echo "  fan|tune|temps <args>   run the matching helper"
    echo "  oc <args>     voltage-frequency curve OC (needs the xe_gt_oc patch)" ;;
  *) echo "unknown: $1 (try: xe-gpu help)"; exit 1;;
esac
