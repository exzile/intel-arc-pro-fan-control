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
for f in xe-fan-curve xe-gpu-tune xe-gpu-temps xe-gpu xe-gpu-oc xe-gpu-stress; do
  if [ -f "scripts/$f.sh" ]; then
    install -m755 "scripts/$f.sh" "$BIN/$f"
    echo "  installed $BIN/$f"
  fi
done
[ -f scripts/xe-fan-rebuild.sh ] && install -m755 scripts/xe-fan-rebuild.sh /usr/local/sbin/xe-fan-rebuild \
  && echo "  installed /usr/local/sbin/xe-fan-rebuild"

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

echo "install.sh: done. Launch 'Arc GPU Dashboard' from the apps menu, or run xe-gpu-gui."
