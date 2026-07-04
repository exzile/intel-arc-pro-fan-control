#!/bin/bash
# xe-gpu-temps — read-only temperature/health monitor for an Intel Arc (xe) GPU.
# Surfaces every sensor the driver exposes (pkg / mctrl / pcie / vram + per-channel vram)
# with their crit/emergency/max limits, plus fan RPM and power. Pure reads — safe, no patch.
#
#   sudo xe-gpu-temps            # one-shot table (sudo only needed for some power nodes)
#   xe-gpu-temps watch [secs]    # live refresh (default 2s)
#   xe-gpu-temps json            # machine-readable, one JSON object per run
set -uo pipefail

# --- find the xe GPU hwmon (name == "xe"), don't hardcode hwmonN ---
HW=""
for d in /sys/class/hwmon/hwmon*; do
  [ -r "$d/name" ] && [ "$(cat "$d/name")" = "xe" ] && { HW="$d"; break; }
done
[ -n "$HW" ] || { echo "error: no xe GPU hwmon found (is the xe driver loaded?)" >&2; exit 1; }

rd() { cat "$1" 2>/dev/null; }
mC() { local v; v=$(rd "$1"); [ -n "$v" ] && echo $(( v / 1000 )) || echo ""; }   # milli-unit -> unit

# collect temp channel numbers, ordered: pkg, mctrl, pcie, vram, then vram_ch_* by index
temp_nums() { for f in "$HW"/temp*_input; do basename "$f" _input | sed 's/temp//'; done | sort -n; }

# a compact bar for a temp vs its crit limit
bar() {
  local t=$1 crit=$2 w=20
  [ -z "$t" ] || [ -z "$crit" ] || [ "$crit" -le 0 ] 2>/dev/null && { printf ""; return; }
  local fill=$(( t * w / crit )); [ "$fill" -gt "$w" ] && fill=$w; [ "$fill" -lt 0 ] && fill=0
  local i s=""
  for ((i=0;i<fill;i++)); do s+="█"; done
  for ((i=fill;i<w;i++)); do s+="·"; done
  printf "%s" "$s"
}

show() {
  local card_drv
  echo "Intel Arc (xe) GPU — temperatures            $(date '+%H:%M:%S')"
  echo "hwmon: $HW"
  echo
  printf "  %-12s %6s   %-20s  %5s %5s %5s\n" "sensor" "temp" "load(vs crit)" "max" "crit" "emrg"
  printf "  %-12s %6s   %-20s  %5s %5s %5s\n" "------" "----" "-------------" "---" "----" "----"
  # hottest tracker
  local hot=-1 hotlbl=""
  for n in $(temp_nums); do
    local lbl inp crit emrg mx
    lbl=$(rd "$HW/temp${n}_label"); [ -z "$lbl" ] && lbl="temp$n"
    inp=$(mC "$HW/temp${n}_input")
    crit=$(mC "$HW/temp${n}_crit"); emrg=$(mC "$HW/temp${n}_emergency"); mx=$(mC "$HW/temp${n}_max")
    [ -z "$inp" ] && continue
    if [ "$inp" -gt "$hot" ] 2>/dev/null; then hot=$inp; hotlbl=$lbl; fi
    local b=""; [ -n "$crit" ] && b=$(bar "$inp" "$crit")
    printf "  %-12s %5s°   %-20s  %5s %5s %5s\n" "$lbl" "$inp" "$b" "${mx:--}" "${crit:--}" "${emrg:--}"
  done
  echo
  # fan + power summary
  local fan pwm pcap pcrit
  fan=$(rd "$HW/fan1_input"); pwm=$(rd "$HW/pwm1_enable")
  pcap=$(rd "$HW/power1_cap"); pcrit=$(rd "$HW/power1_crit")
  local pwmdesc="?"; case "${pwm:-}" in 0) pwmdesc="full-speed";; 1) pwmdesc="manual curve";; 2) pwmdesc="auto-stock";; esac
  printf "  hottest: %s %s°C\n" "$hotlbl" "$hot"
  [ -n "$fan" ] && printf "  fan1   : %s rpm (mode: %s)\n" "$fan" "$pwmdesc"
  { [ -n "$pcap" ] && [ "$pcap" != 0 ]; } && printf "  power  : cap %s W\n" "$(( pcap/1000000 ))"
  [ -n "$pcrit" ] && printf "  I1 crit: %s W\n" "$(awk "BEGIN{printf \"%.2f\", $pcrit/1000000}")"
}

json() {
  local first=1
  printf '{"sensors":['
  for n in $(temp_nums); do
    local lbl inp crit emrg mx
    lbl=$(rd "$HW/temp${n}_label"); [ -z "$lbl" ] && lbl="temp$n"
    inp=$(mC "$HW/temp${n}_input"); [ -z "$inp" ] && continue
    crit=$(mC "$HW/temp${n}_crit"); emrg=$(mC "$HW/temp${n}_emergency"); mx=$(mC "$HW/temp${n}_max")
    [ $first -eq 0 ] && printf ','
    printf '{"label":"%s","temp_c":%s,"max_c":%s,"crit_c":%s,"emergency_c":%s}' \
      "$lbl" "$inp" "${mx:-null}" "${crit:-null}" "${emrg:-null}"
    first=0
  done
  printf '],"fan1_rpm":%s,"pwm1_enable":%s}\n' "$(rd "$HW/fan1_input" || echo null)" "$(rd "$HW/pwm1_enable" || echo null)"
}

case "${1:-show}" in
  show) show ;;
  json) json ;;
  watch)
    secs="${2:-2}"
    trap 'echo; exit 0' INT
    while true; do clear 2>/dev/null; show; echo; echo "  (watch ${secs}s — Ctrl-C to stop)"; sleep "$secs"; done ;;
  *) echo "usage: xe-gpu-temps {show|watch [secs]|json}"; exit 1;;
esac
