#!/bin/bash
# xe-fan-curve — set a custom fan curve on an Intel Arc (xe) GPU on Linux.
# Requires the series-168027 fan-control patch (pwm1_auto_point* sysfs) + root.
#
# Usage:
#   sudo xe-fan-curve auto                 # hand fan back to the GPU's stock curve
#   sudo xe-fan-curve max                  # full speed
#   sudo xe-fan-curve set  T:PWM  T:PWM ...  # custom curve (temp °C : pwm 0-255)
#   sudo xe-fan-curve show                 # print current mode + curve + RPM
#
# Example (fan off till 45C, then ramp):
#   sudo xe-fan-curve set 45:80 55:120 65:170 75:210 85:255
#
# Rules the script handles for you:
#  - switches to manual mode (pwm1_enable=1) BEFORE writing points
#  - pads/repeats your points to fill all 10 slots, kept monotonic
#  - never toggles back to mode 2 mid-write (that would wipe the user curve)

set -euo pipefail

# --- locate the xe hwmon directory by name (don't hardcode hwmonN) ---
HWMON=""
for d in /sys/class/hwmon/hwmon*; do
  [ -r "$d/name" ] || continue
  if [ "$(cat "$d/name")" = "xe" ]; then HWMON="$d"; break; fi
done
[ -n "$HWMON" ] || { echo "error: no xe hwmon device found (is the patched xe module loaded?)"; exit 1; }
[ -e "$HWMON/pwm1_enable" ] || { echo "error: $HWMON has no pwm1_enable (fan-control patch not active)"; exit 1; }

NPOINTS=0
while [ -e "$HWMON/pwm1_auto_point$((NPOINTS+1))_temp" ]; do NPOINTS=$((NPOINTS+1)); done

show() {
  local mode; mode=$(cat "$HWMON/pwm1_enable")
  local modestr=("full-speed" "manual-user-curve" "auto-stock")
  echo "device : $HWMON  (fan slots: $NPOINTS)"
  echo "mode   : $mode (${modestr[$mode]:-?})"
  echo "rpm    : $(cat "$HWMON/fan1_input")   (max $(cat "$HWMON/fan1_max" 2>/dev/null || echo '?'))"
  echo "temp   : $(( $(cat "$HWMON/temp2_input") / 1000 )) C"
  echo "curve  :"
  for i in $(seq 1 "$NPOINTS"); do
    printf "  point %2d: %3d C -> pwm %3d\n" "$i" \
      "$(( $(cat "$HWMON/pwm1_auto_point${i}_temp") / 1000 ))" \
      "$(cat "$HWMON/pwm1_auto_point${i}_pwm")"
  done
}

case "${1:-show}" in
  show) show ;;
  auto) echo 2 > "$HWMON/pwm1_enable"; echo "fan handed back to stock auto curve."; ;;
  max)  echo 0 > "$HWMON/pwm1_enable"; echo "fan set to FULL SPEED."; ;;
  stock-read)
    # Print the card's STOCK fan curve as "STOCK: T:PWM ..." WITHOUT changing the applied curve.
    # There is no direct sysfs for the stock table; entering manual mode FROM auto copies stock
    # into the user table, so we snapshot the user table, do that flip to reveal stock, read it,
    # then restore the user's own curve + mode.
    saved=""; for i in $(seq 1 "$NPOINTS"); do
      t=$(cat "$HWMON/pwm1_auto_point${i}_temp" 2>/dev/null) || break
      saved="$saved $(( t/1000 )):$(cat "$HWMON/pwm1_auto_point${i}_pwm")"
    done
    ena=$(cat "$HWMON/pwm1_enable")
    echo 2 > "$HWMON/pwm1_enable" 2>/dev/null
    echo 1 > "$HWMON/pwm1_enable" 2>/dev/null      # copies stock -> user table
    stock=""; for i in $(seq 1 "$NPOINTS"); do
      t=$(cat "$HWMON/pwm1_auto_point${i}_temp" 2>/dev/null) || break
      stock="$stock $(( t/1000 )):$(cat "$HWMON/pwm1_auto_point${i}_pwm")"
    done
    [ -n "$saved" ] && "$0" set $saved >/dev/null 2>&1   # restore the user's curve
    [ "$ena" = 1 ] || echo "$ena" > "$HWMON/pwm1_enable" 2>/dev/null
    echo "STOCK:$stock"
    ;;
  set)
    shift
    [ $# -ge 1 ] || { echo "give at least one T:PWM pair"; exit 1; }
    # parse pairs into arrays
    declare -a TT PP
    for pair in "$@"; do
      t="${pair%%:*}"; p="${pair##*:}"
      [[ "$t" =~ ^[0-9]+$ && "$p" =~ ^[0-9]+$ ]] || { echo "bad pair '$pair' (use T:PWM, e.g. 55:120)"; exit 1; }
      [ "$p" -le 255 ] || { echo "pwm $p > 255"; exit 1; }
      TT+=("$t"); PP+=("$p")
    done
    n=${#TT[@]}
    # manual mode FIRST (auto_point writes are rejected otherwise). Only switch if
    # needed — the driver returns EINVAL on writing pwm1_enable=1 when already 1.
    [ "$(cat "$HWMON/pwm1_enable" 2>/dev/null)" = 1 ] || echo 1 > "$HWMON/pwm1_enable"
    # Fill the fan-table slots: given points, then repeat the last (temp climbs +1C, pwm held).
    # Two hardware rules the driver enforces (else EINVAL on the committing write):
    #  - BOTH temp AND pwm must be non-decreasing across points -> clamp each >= the previous.
    #  - only the first `point_count` slots are writable even though 10 sysfs files exist; the
    #    firmware commits the table when the LAST valid point is written. Writing past that count
    #    EINVALs *after* the curve is already applied, so tolerate a trailing write failure and
    #    only fail if fewer than the user's own points were accepted.
    prev_t=-1; prev_p=0; applied=0
    for i in $(seq 1 "$NPOINTS"); do
      if [ "$i" -le "$n" ]; then
        t=${TT[$((i-1))]}; p=${PP[$((i-1))]}
      else
        t=$(( prev_t/1000 + 1 )); p=${PP[$((n-1))]}   # pad above last point
      fi
      tm=$(( t*1000 ))
      [ "$tm" -gt "$prev_t" ] || tm=$(( prev_t + 1000 ))   # strictly increasing temp
      [ "$p"  -ge "$prev_p" ] || p=$prev_p                 # non-decreasing pwm
      echo "$tm" > "$HWMON/pwm1_auto_point${i}_temp" 2>/dev/null || true
      if ! echo "$p" > "$HWMON/pwm1_auto_point${i}_pwm" 2>/dev/null; then
        break   # slot beyond the hardware point count; the table already committed
      fi
      applied=$i; prev_t=$tm; prev_p=$p
    done
    [ "$applied" -ge "$n" ] || { echo "error: only $applied of $n points were accepted"; exit 1; }
    echo "applied custom curve:"; show
    ;;
  boot)
    # Applied at boot by the systemd service. Reads CURVE="T:PWM ..." from the conf.
    CONF=/etc/xe-fan-curve.conf
    [ -r "$CONF" ] || { echo "no $CONF; leaving fan on auto-stock"; echo 2 > "$HWMON/pwm1_enable"; exit 0; }
    # shellcheck disable=SC1090
    CURVE=""; . "$CONF"
    [ -n "$CURVE" ] || { echo "empty CURVE in $CONF; auto-stock"; echo 2 > "$HWMON/pwm1_enable"; exit 0; }
    # reuse 'set' with the configured pairs
    exec "$0" set $CURVE
    ;;
  *) echo "usage: xe-fan-curve {show|auto|max|set T:PWM ...|boot}"; exit 1 ;;
esac
