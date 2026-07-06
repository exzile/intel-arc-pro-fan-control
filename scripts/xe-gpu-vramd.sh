#!/usr/bin/env bash
# xe-gpu-vramd.sh - tiny VRAM-usage exporter.
#
# xe reports VRAM used/total only in root-only debugfs (tile0/vram_mm). This daemon
# reads it and writes "<used_bytes> <total_bytes>" to a world-readable /run file so the
# unprivileged GUI can show a live VRAM-usage metric without elevating on every poll.
# It exposes ONLY the two VRAM numbers — nothing else from debugfs. Runs as root via
# xe-gpu-vram.service. Interval (seconds) optional, default 2.
set -uo pipefail

list_bdfs(){   # every xe card's PCI address, one per line
  local d drv
  for d in /sys/class/drm/card*/device; do
    drv=$(basename "$(readlink -f "$d/driver" 2>/dev/null)" 2>/dev/null)
    [ "$drv" = xe ] && basename "$(readlink -f "$d")"
  done | sort -u
}

read_vram(){   # $1=bdf -> "used total" (bytes) or empty
  local f="/sys/kernel/debug/dri/$1/tile0/vram_mm" used="" total="" k v _
  [ -r "$f" ] || return 0
  while read -r k v _; do
    case "$k" in
      size:)  [ -z "$total" ] && total="$v" ;;
      usage:) [ -z "$used" ] && used="$v" ;;
    esac
    [ -n "$total" ] && [ -n "$used" ] && break
  done < "$f"
  [ -n "$used" ] && [ -n "$total" ] && printf '%s %s' "$used" "$total"
}

INTERVAL="${1:-2}"

while :; do
  first=1
  for bdf in $(list_bdfs); do
    v=$(read_vram "$bdf") || v=""
    [ -n "$v" ] || continue
    printf '%s\n' "$v" > "/run/xe-gpu-vram-$bdf.tmp" && mv "/run/xe-gpu-vram-$bdf.tmp" "/run/xe-gpu-vram-$bdf" \
      && chmod 644 "/run/xe-gpu-vram-$bdf"
    if [ "$first" = 1 ]; then      # first card also to the default file (back-compat)
      printf '%s\n' "$v" > /run/xe-gpu-vram.tmp && mv /run/xe-gpu-vram.tmp /run/xe-gpu-vram && chmod 644 /run/xe-gpu-vram
      first=0
    fi
  done
  sleep "$INTERVAL"
done
