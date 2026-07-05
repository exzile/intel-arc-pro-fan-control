#!/usr/bin/env bash
# xe-gpu-vramd.sh - tiny VRAM-usage exporter.
#
# xe reports VRAM used/total only in root-only debugfs (tile0/vram_mm). This daemon
# reads it and writes "<used_bytes> <total_bytes>" to a world-readable /run file so the
# unprivileged GUI can show a live VRAM-usage metric without elevating on every poll.
# It exposes ONLY the two VRAM numbers — nothing else from debugfs. Runs as root via
# xe-gpu-vram.service. Interval (seconds) optional, default 2.
set -uo pipefail

_detect_bdf(){
  local d drv
  for d in /sys/class/drm/card*/device; do
    drv=$(basename "$(readlink -f "$d/driver" 2>/dev/null)" 2>/dev/null)
    [ "$drv" = xe ] && { basename "$(readlink -f "$d")"; return 0; }
  done
  return 1
}

BDF="${ARC_GPU_BDF:-$(_detect_bdf || echo 0000:03:00.0)}"
FILE="/sys/kernel/debug/dri/$BDF/tile0/vram_mm"
OUT=/run/xe-gpu-vram
INTERVAL="${1:-2}"

while :; do
  used=""; total=""
  if [ -r "$FILE" ]; then
    # first "size:" / "usage:" lines are the vram region's total / used (bytes)
    while read -r k v _; do
      case "$k" in
        size:)  [ -z "$total" ] && total="$v" ;;
        usage:) [ -z "$used" ] && used="$v" ;;
      esac
      [ -n "$total" ] && [ -n "$used" ] && break
    done < "$FILE"
  fi
  if [ -n "$used" ] && [ -n "$total" ]; then
    printf '%s %s\n' "$used" "$total" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT" && chmod 644 "$OUT"
  fi
  sleep "$INTERVAL"
done
