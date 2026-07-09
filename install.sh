#!/usr/bin/env bash
# install.sh - install/refresh the Arc userland helpers + GUI from this checkout.
#
# Idempotent: copies the CLI tools to /usr/local/bin, the GUI to /usr/local/bin,
# and the desktop entry for the current user. Does NOT touch the kernel module
# (that's the separate apply_xefan.sh / apply_xeoc.sh flow). Run after a git pull:
#
#   git pull && sudo bash install.sh
#
# so the box always matches the repo. Run as root (the /usr/local/bin copies need it);
# the per-user .desktop file is installed for the invoking user via $SUDO_USER.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

[ "$(id -u)" -eq 0 ] || { echo "install.sh: run with sudo (needs /usr/local/bin)"; exit 1; }

# CLI helpers -> /usr/local/bin (basename without the .sh)
BIN=/usr/local/bin
for f in xe-fan-curve xe-gpu-tune xe-gpu-temps xe-gpu xe-gpu-oc xe-gpu-stress xe-gpu-vramd; do
  if [ -f "scripts/$f.sh" ]; then
    install -m755 "scripts/$f.sh" "$BIN/$f"
    echo "  installed $BIN/$f"
  fi
done
[ -f scripts/xe-fan-rebuild.sh ] && install -m755 scripts/xe-fan-rebuild.sh /usr/local/sbin/xe-fan-rebuild \
  && echo "  installed /usr/local/sbin/xe-fan-rebuild"
# optional LLM/bandwidth benchmark setup (the GUI runs this on benchmark opt-in)
[ -f scripts/setup-llm-benchmark.sh ] && install -m755 scripts/setup-llm-benchmark.sh "$BIN/xe-gpu-benchmark-setup" \
  && echo "  installed $BIN/xe-gpu-benchmark-setup"

# GUI -> /usr/local/bin
if [ -f gui/xe-gpu-gui.py ]; then
  install -m755 gui/xe-gpu-gui.py "$BIN/xe-gpu-gui"
  echo "  installed $BIN/xe-gpu-gui"
fi

# desktop entry for the invoking (non-root) user, so it shows in the apps menu
USER_HOME=$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)
if [ -f gui/xe-gpu-gui.desktop ] && [ -n "$USER_HOME" ]; then
  APPS="$USER_HOME/.local/share/applications"
  install -D -m644 gui/xe-gpu-gui.desktop "$APPS/xe-gpu-gui.desktop"
  chown "${SUDO_USER:-$USER}" "$APPS/xe-gpu-gui.desktop" 2>/dev/null || true
  update-desktop-database "$APPS" 2>/dev/null || true
  echo "  installed $APPS/xe-gpu-gui.desktop"
fi

# VRAM-usage exporter service (exposes only the VRAM figure from root-only debugfs
# to /run/xe-gpu-vram so the GUI can show a live VRAM-usage metric)
if [ -f systemd/xe-gpu-vram.service ] && command -v systemctl >/dev/null 2>&1; then
  install -m644 systemd/xe-gpu-vram.service /etc/systemd/system/xe-gpu-vram.service
  systemctl daemon-reload
  systemctl enable --now xe-gpu-vram.service 2>/dev/null || true
  echo "  installed + enabled xe-gpu-vram.service (VRAM usage -> /run/xe-gpu-vram)"
fi

# B70 (Battlemage G31, 8086:e223) resizable-BAR workaround for Above-4G-less
# platforms: the B70 POSTs a 32GB VRAM BAR that can't be mapped, so xe fails to
# bind it. The service shrinks the BAR to 256MB and kexecs so the kernel
# re-enumerates it (see docs/B70-G31-MULTI-GPU.md). Only enabled where a B70 is
# present but unbound (i.e. actually affected); needs kexec-tools.
if [ -f scripts/xe-b70-rebar-kexec.sh ] && command -v systemctl >/dev/null 2>&1; then
  install -m755 scripts/xe-b70-rebar-kexec.sh /usr/local/sbin/xe-b70-rebar-kexec.sh
  install -m644 systemd/xe-b70-rebar.service /etc/systemd/system/xe-b70-rebar.service
  systemctl daemon-reload
  b70="$(grep -il '^0xe223$' /sys/bus/pci/devices/*/device 2>/dev/null | head -1)"
  if [ -n "$b70" ] && ! [ -e "$(dirname "$b70")/driver" ] && command -v kexec >/dev/null 2>&1; then
    systemctl enable xe-b70-rebar.service 2>/dev/null || true
    echo "  installed + enabled xe-b70-rebar.service (B70 e223 unbound: BAR-shrink + kexec on boot)"
  elif [ -n "$b70" ] && ! command -v kexec >/dev/null 2>&1; then
    echo "  installed xe-b70-rebar.service but NOT enabled: B70 present but kexec-tools missing (apt install kexec-tools)"
  else
    echo "  installed xe-b70-rebar.service (not enabled: no unbound B70/e223 detected)"
  fi
fi

echo "install.sh: done. Launch 'Arc GPU Dashboard' from the apps menu, or run xe-gpu-gui."
