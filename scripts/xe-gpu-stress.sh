#!/usr/bin/env bash
# xe-gpu-stress.sh - short GPU load + telemetry watch to validate an overclock.
#
# Runs a GL/Vulkan workload for N seconds while sampling GPU clock and package
# temperature, then flags INSTABILITY (a GPU hang/reset, or the workload crashing
# under load) and THROTTLING (peak temp reaching the throttle limit). Emits
# per-second "PROGRESS <sec> <mhz> <tempC>" lines and a machine-readable summary
# (STATUS=ok|throttled|unstable|no_workload, plus MAXTEMP/MINFREQ/...).
#
# Usage: xe-gpu-stress <seconds> [--fan-guard --user <u> --display <d>
#                                 --wayland <w> --runtime <dir>]
# Plain (unprivileged) runs the workload directly. With --fan-guard (run as root,
# e.g. via pkexec) it ramps the fan to MAX for the duration and restores it after,
# launching the workload as <user> with the passed session env so it can open the
# display. Exit: 0 stable/throttled, 2 unstable, 3 no workload, 64 usage.
set -uo pipefail

SECS="${1:-60}"
[[ "$SECS" =~ ^[0-9]+$ ]] || { echo "usage: xe-gpu-stress <seconds> [--fan-guard ...]"; exit 64; }
shift || true
RUNUSER=""; DISP=""; WLD=""; XRD=""; FANGUARD=0; DRIPRIME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --fan-guard) FANGUARD=1; shift ;;
    --user)      RUNUSER="${2:-}"; shift 2 ;;
    --display)   DISP="${2:-}"; shift 2 ;;
    --wayland)   WLD="${2:-}"; shift 2 ;;
    --runtime)   XRD="${2:-}"; shift 2 ;;
    --dri)       DRIPRIME="${2:-}"; shift 2 ;;   # DRI_PRIME to pin the load to a card
    *) shift ;;
  esac
done

_detect_bdf(){   # find the Arc (xe) GPU's PCI address (override with ARC_GPU_BDF)
  local d drv
  for d in /sys/class/drm/card*/device; do
    drv=$(basename "$(readlink -f "$d/driver" 2>/dev/null)" 2>/dev/null)
    [ "$drv" = xe ] && { basename "$(readlink -f "$d")"; return 0; }
  done
  return 1
}
GPU="${ARC_GPU_BDF:-$(_detect_bdf || echo 0000:03:00.0)}"
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

# fan guard: ramp to full speed during the test, restore the prior mode after.
# Needs root + the hwmon pwm1_enable node (0=full, 1=manual curve, 2=auto).
FANPREV=""
fan_max_on(){
  [ "$FANGUARD" = 1 ] && [ "$(id -u)" -eq 0 ] || return 0
  [ -n "$HW" ] && [ -w "$HW/pwm1_enable" ] || return 0
  FANPREV=$(cat "$HW/pwm1_enable" 2>/dev/null)
  echo 0 > "$HW/pwm1_enable" 2>/dev/null && echo "FAN=max"
}
fan_restore(){
  [ -n "$FANPREV" ] && [ -n "$HW" ] && [ -w "$HW/pwm1_enable" ] || return 0
  echo "$FANPREV" > "$HW/pwm1_enable" 2>/dev/null
}
trap fan_restore EXIT

# choose a display workload appropriate for the session. The plain `glmark2` is
# the X11/GLX build and will NOT open a display on a pure Wayland session, so on
# Wayland (WLD set) prefer the Wayland-native binaries; vkmark (Vulkan) works on
# both. glxgears is a weak last resort.
if [ -n "$WLD" ]; then
  CANDS="glmark2-wayland vkmark glmark2-es2-wayland glmark2 glxgears"   # Wayland session
else
  CANDS="glmark2 vkmark glmark2-x11 glxgears"                            # X11 / XWayland
fi
WL=""; WLNAME=""
for b in $CANDS; do
  if command -v "$b" >/dev/null 2>&1; then
    case "$b" in glxgears) WL="$b" ;; *) WL="$b --run-forever" ;; esac
    WLNAME="$b"; break
  fi
done
if [ -z "$WL" ]; then
  echo "STATUS=no_workload"
  echo "install a GPU load generator (glmark2-wayland / vkmark) to run a stability test"
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

# ramp the fan to max for the test (restored on exit via the trap)
fan_max_on

# launch the workload in its own session (so we can kill it + children), with vsync
# uncapped (vblank_mode=0) so it actually loads the GPU rather than idling at 60 fps.
# Under --fan-guard we run as root, so drop to the target user (with their session
# env) to open the display; otherwise run the workload directly.
DRIENV=""; [ -n "$DRIPRIME" ] && DRIENV="DRI_PRIME=$DRIPRIME"
if [ "$FANGUARD" = 1 ] && [ "$(id -u)" -eq 0 ] && [ -n "$RUNUSER" ]; then
  setsid runuser -u "$RUNUSER" -- env DISPLAY="$DISP" WAYLAND_DISPLAY="$WLD" \
    XDG_RUNTIME_DIR="$XRD" $DRIENV vblank_mode=0 __GL_SYNC_TO_VBLANK=0 $WL >/dev/null 2>&1 &
else
  setsid env $DRIENV vblank_mode=0 __GL_SYNC_TO_VBLANK=0 $WL >/dev/null 2>&1 &
fi
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
