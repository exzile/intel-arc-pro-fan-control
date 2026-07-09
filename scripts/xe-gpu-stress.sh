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
RUNUSER=""; DISP=""; WLD=""; XRD=""; FANGUARD=0; DRIPRIME=""; OC_CURVE=""; OC_MEM=""; OC_TEMP=""; WLOUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --fan-guard) FANGUARD=1; shift ;;
    --user)      RUNUSER="${2:-}"; shift 2 ;;
    --display)   DISP="${2:-}"; shift 2 ;;
    --wayland)   WLD="${2:-}"; shift 2 ;;
    --runtime)   XRD="${2:-}"; shift 2 ;;
    --dri)       DRIPRIME="${2:-}"; shift 2 ;;   # DRI_PRIME to pin the load to a card
    --oc-curve)  OC_CURVE="${2:-}"; shift 2 ;;   # test these VF-curve points (i:mv ...) transiently
    --oc-mem)    OC_MEM="${2:-}";   shift 2 ;;   # test this memory speed (Mbps) transiently
    --oc-temp)   OC_TEMP="${2:-}";  shift 2 ;;   # test this temp limit (degC) transiently
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
OCV="$DEV/tile0/gt0/oc/vf_curve"
OCM="$DEV/tile0/gt0/oc/mem_speed"

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

# Temporarily apply the OC settings under test DIRECTLY to sysfs (NOT persisted).
# Snapshot the live values first, restore them on exit. Transient by design: if
# the OC hangs the GPU, nothing was written to /etc/xe-gpu-oc.conf, so the box
# boots back on the previous known-good settings.
OC_TESTING=0; OC_S_CURVE=""; OC_S_MEM=""; OC_S_TEMP=""
oc_test_apply(){
  [ "$(id -u)" -eq 0 ] || return 0
  [ -n "$OC_CURVE$OC_MEM$OC_TEMP" ] || return 0
  [ -w "$OCV" ] || return 0
  OC_S_CURVE=$(awk '{printf "%s:%s ", $1, $2}' "$OCV" 2>/dev/null)
  [ -r "$OCM" ] && OC_S_MEM=$(cat "$OCM" 2>/dev/null)
  [ -r "$TL"  ] && OC_S_TEMP=$(cat "$TL" 2>/dev/null)
  OC_TESTING=1
  local p
  for p in $OC_CURVE; do echo "${p%%:*} ${p##*:}" > "$OCV" 2>/dev/null; done
  [ -n "$OC_MEM"  ] && echo "$OC_MEM"  > "$OCM" 2>/dev/null
  [ -n "$OC_TEMP" ] && echo "$OC_TEMP" > "$TL"  2>/dev/null
  echo "OC=under-test"
}
oc_test_restore(){
  [ "$OC_TESTING" = 1 ] || return 0
  local p
  for p in $OC_S_CURVE; do echo "${p%%:*} ${p##*:}" > "$OCV" 2>/dev/null; done
  [ -n "$OC_S_MEM"  ] && echo "$OC_S_MEM"  > "$OCM" 2>/dev/null
  [ -n "$OC_S_TEMP" ] && echo "$OC_S_TEMP" > "$TL"  2>/dev/null
}
trap 'oc_test_restore; fan_restore; rm -f "$WLOUT" 2>/dev/null' EXIT

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
# apply the OC settings under test transiently (restored on exit via the trap)
oc_test_apply

# launch the workload in its own session (so we can kill it + children), with vsync
# uncapped (vblank_mode=0) so it actually loads the GPU rather than idling at 60 fps.
# Under --fan-guard we run as root, so drop to the target user (with their session
# env) to open the display; otherwise run the workload directly.
DRIENV=""; [ -n "$DRIPRIME" ] && DRIENV="DRI_PRIME=$DRIPRIME"
# Capture the workload's stdout (line-buffered via stdbuf so FPS lines land during
# the run, surviving the kill) to derive a benchmark score afterward.
WLOUT="$(mktemp /tmp/xe-gpu-stress.XXXXXX 2>/dev/null || echo /tmp/xe-gpu-stress.out)"
if [ "$FANGUARD" = 1 ] && [ "$(id -u)" -eq 0 ] && [ -n "$RUNUSER" ]; then
  setsid runuser -u "$RUNUSER" -- env DISPLAY="$DISP" WAYLAND_DISPLAY="$WLD" \
    XDG_RUNTIME_DIR="$XRD" $DRIENV vblank_mode=0 __GL_SYNC_TO_VBLANK=0 stdbuf -oL $WL >"$WLOUT" 2>&1 &
else
  setsid env $DRIENV vblank_mode=0 __GL_SYNC_TO_VBLANK=0 stdbuf -oL $WL >"$WLOUT" 2>&1 &
fi
PID=$!
sleep 1

TLIMIT=$( [ -r "$TL" ] && cat "$TL" 2>/dev/null || echo 100 )
MAXT=0; MINF=999999; MAXF=0; CRASH=0; i=0; FSUM=0
E0=$( [ -n "$HW" ] && cat "$HW/energy1_input" 2>/dev/null || echo 0 )   # for average power
while [ "$i" -lt "$SECS" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then CRASH=1; break; fi
  mhz=$(cat "$FREQ" 2>/dev/null || echo 0)
  tc=$(( $(pkg_temp_mC) / 1000 ))
  (( tc > MAXT )) && MAXT=$tc
  (( i >= 5 && mhz < MINF )) && MINF=$mhz   # ignore warm-up ramp for the low-clock floor
  (( mhz > MAXF )) && MAXF=$mhz
  FSUM=$((FSUM + mhz))
  echo "PROGRESS $((i + 1)) $mhz $tc"
  i=$((i + 1)); sleep 1
done

kill "$PID" 2>/dev/null; kill -- -"$PID" 2>/dev/null; wait "$PID" 2>/dev/null
[ "$MINF" = 999999 ] && MINF=0

# average clock + average power (from the energy counter delta over the run)
AVGF=$(( i > 0 ? FSUM / i : 0 ))
E1=$( [ -n "$HW" ] && cat "$HW/energy1_input" 2>/dev/null || echo 0 )
AVGP=$(awk -v a="$E0" -v b="$E1" -v n="$i" 'BEGIN{ if (n>0 && b>a) printf "%.0f", (b-a)/n/1000000; else print 0 }')
# memory speed actually in effect (Mbps) — confirms the mem OC took
MEMSPEED=$( [ -r "$OCM" ] && cat "$OCM" 2>/dev/null || echo 0 )
# measured VRAM bandwidth via clpeak (OpenCL), if installed — directly reflects the
# memory OC. Bandwidth is clpeak's FIRST section, so a short timeout captures it.
MEMBW=""; COMPUTE=""
if command -v clpeak >/dev/null 2>&1; then
  CLP=$(timeout 40 clpeak 2>/dev/null)    # bandwidth (LLM decode) + compute (LLM prefill)
  MEMBW=$(printf '%s\n' "$CLP"   | awk '/Global memory bandwidth/{f=1;next} /^[[:space:]]*$/{f=0} f{print $NF}' | sort -g | tail -1)
  COMPUTE=$(printf '%s\n' "$CLP" | awk '/Single-precision compute/{f=1;next}  /^[[:space:]]*$/{f=0} f{print $NF}' | sort -g | tail -1)
  MEMBW=$(printf '%.0f' "${MEMBW:-0}" 2>/dev/null);     [ "$MEMBW" = 0 ] && MEMBW=""
  COMPUTE=$(printf '%.0f' "${COMPUTE:-0}" 2>/dev/null); [ "$COMPUTE" = 0 ] && COMPUTE=""
fi

# real LLM throughput on the GPU via OpenVINO GenAI, if set up (see
# scripts/setup-llm-benchmark.sh): prefill (compute-bound) + decode (memory-
# bandwidth-bound) tok/s, measured with the OC still applied.
LLMPRE=""; LLMDEC=""
LLMH="/home/${RUNUSER:-joey}/ovbench"
if [ -x "$LLMH/bin/python" ] && [ -f "$LLMH/llmbench.py" ] && [ -d "$LLMH/model" ]; then
  LO=$(timeout 90 "$LLMH/bin/python" "$LLMH/llmbench.py" "$LLMH/model" GPU 2>/dev/null)
  LLMPRE=$(printf '%s\n' "$LO" | awk -F= '/^PREFILL=/{printf "%.0f",$2; exit}')
  LLMDEC=$(printf '%s\n' "$LO" | awk -F= '/^DECODE=/{printf "%.0f",$2; exit}')
fi

# benchmark score = average of the FPS figures the load tool reported (glmark2 /
# vkmark print "FPS: N" per scene). Same tool + scenes each run, so it's a valid
# apples-to-apples number to compare across settings. glxgears prints none -> empty.
SCORE=""
if [ -s "$WLOUT" ]; then
  SCORE=$(grep -oiE 'FPS:?[[:space:]]+[0-9]+' "$WLOUT" 2>/dev/null | grep -oE '[0-9]+$' \
          | awk '{s+=$1; n++} END{if (n) printf "%d", s/n}')
fi
rm -f "$WLOUT"

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
[ -n "$SCORE" ] && { echo "SCORE=$SCORE"; echo "SCOREUNIT=fps"; }
echo "AVGFREQ=$AVGF"
echo "AVGPOWER=$AVGP"
[ "$MEMSPEED" != 0 ] && echo "MEMSPEED=$MEMSPEED"
[ -n "$MEMBW" ] && echo "MEMBW=$MEMBW"
[ -n "$COMPUTE" ] && echo "COMPUTE=$COMPUTE"
[ -n "$LLMPRE" ] && echo "LLMPREFILL=$LLMPRE"
[ -n "$LLMDEC" ] && echo "LLMDECODE=$LLMDEC"
case "$ST" in
  ok)        echo "stable: ${SECS}s load, peak ${MAXT}C, clocks ${MINF}-${MAXF} MHz, no hang"; exit 0 ;;
  throttled) echo "throttled: hit ${MAXT}C (limit ${TLIMIT}C) - stable but thermally capped";  exit 0 ;;
  unstable)  echo "UNSTABLE: GPU hang/crash detected under load";                                exit 2 ;;
esac
