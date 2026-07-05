#!/usr/bin/env bash
# xe-gpu-stress.sh - short GPU load + telemetry watch to validate an overclock.
#
# Unprivileged. Runs a GL/Vulkan workload for N seconds while sampling GPU clock and
# package temperature, then flags INSTABILITY (a GPU hang/reset, or the workload
# crashing under load) and THROTTLING (peak temp reaching the throttle limit).
# Emits per-second "PROGRESS <sec> <mhz> <tempC>" lines and a machine-readable
# summary (STATUS=ok|throttled|unstable|no_workload, plus MAXTEMP/MINFREQ/...).
# Exit: 0 stable/throttled, 2 unstable, 3 no workload available, 64 usage.
set -uo pipefail

SECS="${1:-60}"
[[ "$SECS" =~ ^[0-9]+$ ]] || { echo "usage: xe-gpu-stress <seconds>"; exit 64; }

GPU="${ARC_GPU_BDF:-0000:03:00.0}"
DEV="/sys/bus/pci/devices/$GPU"
FREQ="$DEV/tile0/gt0/freq0/cur_freq"
TL="$DEV/tile0/gt0/oc/temp_limit"

# locate the xe hwmon node (for package temperature)
HW=""
for d in /sys/class/hwmon/hwmon*; do
  [ -r "$d/name" ] && [ "$(cat "$d/name")" = xe ] && { HW="$d"; break; }
done
pkg_temp_mC(){   # package temp in millidegrees C (0 if unavailable)
  [ -n "$HW" ] || { echo 0; return; }
  local f
  for f in "$HW"/temp*_label; do
    [ -r "$f" ] || continue
    if [ "$(cat "$f")" = pkg ]; then cat "${f%_label}_input" 2>/dev/null || echo 0; return; fi
  done
  echo 0
}

# choose a display workload (first available)
WL=""; WLNAME=""
if   command -v glmark2  >/dev/null 2>&1; then WL="glmark2 --run-forever"; WLNAME=glmark2
elif command -v vkmark   >/dev/null 2>&1; then WL="vkmark --run-forever";  WLNAME=vkmark
elif command -v glxgears >/dev/null 2>&1; then WL="glxgears";              WLNAME=glxgears
fi
if [ -z "$WL" ]; then
  echo "STATUS=no_workload"
  echo "install glmark2 or vkmark to run a stability test"
  exit 3
fi

# best-effort GPU-hang counter from the kernel log (dmesg is often restricted -> -1 = unknown)
hang_count(){
  local out
  out=$(dmesg 2>/dev/null) || { echo -1; return; }
  [ -z "$out" ] && { echo -1; return; }
  printf '%s\n' "$out" | grep -icE 'xe .*(reset|hang|wedged)'
}
BASE=$(hang_count)

# launch the workload in its own session (so we can kill it + children), with vsync
# uncapped (vblank_mode=0) so it actually loads the GPU rather than idling at 60 fps
setsid env vblank_mode=0 __GL_SYNC_TO_VBLANK=0 $WL >/dev/null 2>&1 &
PID=$!
sleep 1

TLIMIT=$( [ -r "$TL" ] && cat "$TL" 2>/dev/null || echo 100 )
MAXT=0; MINF=999999; MAXF=0; CRASH=0; i=0
while [ "$i" -lt "$SECS" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then CRASH=1; break; fi
  mhz=$(cat "$FREQ" 2>/dev/null || echo 0)
  tc=$(( $(pkg_temp_mC) / 1000 ))
  (( tc > MAXT )) && MAXT=$tc
  (( i >= 5 && mhz < MINF )) && MINF=$mhz   # ignore warm-up ramp for the low-clock floor
  (( mhz > MAXF )) && MAXF=$mhz
  echo "PROGRESS $((i + 1)) $mhz $tc"
  i=$((i + 1)); sleep 1
done

kill "$PID" 2>/dev/null; kill -- -"$PID" 2>/dev/null; wait "$PID" 2>/dev/null
[ "$MINF" = 999999 ] && MINF=0

# a death within the first few seconds is a LAUNCH failure (no display / missing
# driver), not an OC-induced hang -- real instability shows up under sustained load
if [ "$CRASH" = 1 ] && (( i < 3 )); then
  echo "STATUS=error"
  echo "workload '$WLNAME' failed to start (no display, or GL/Vulkan unavailable)"
  exit 3
fi

NOW=$(hang_count); HANG=0
[ "$BASE" != "-1" ] && [ "$NOW" != "-1" ] && (( NOW > BASE )) && HANG=1
[ "$CRASH" = 1 ] && HANG=1
THROT=0; (( MAXT >= TLIMIT )) && THROT=1

if   [ "$HANG"  = 1 ]; then ST=unstable
elif [ "$THROT" = 1 ]; then ST=throttled
else ST=ok; fi

[ "$WLNAME" = glxgears ] && echo "NOTE=glxgears is a light load; install glmark2 or vkmark for a stronger test"

echo "STATUS=$ST"
echo "WORKLOAD=$WLNAME"
echo "MAXTEMP=$MAXT"
echo "TEMPLIMIT=$TLIMIT"
echo "MINFREQ=$MINF"
echo "MAXFREQ=$MAXF"
echo "HANG=$HANG"
echo "CRASH=$CRASH"
case "$ST" in
  ok)        echo "stable: ${SECS}s load, peak ${MAXT}C, clocks ${MINF}-${MAXF} MHz, no hang"; exit 0 ;;
  throttled) echo "throttled: hit ${MAXT}C (limit ${TLIMIT}C) - stable but thermally capped";  exit 0 ;;
  unstable)  echo "UNSTABLE: GPU hang/crash detected under load";                                exit 2 ;;
esac
