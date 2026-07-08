#!/usr/bin/env bash
# build-xe-module.sh — build & install the patched `xe` driver that exposes
# fan control (pwm sysfs) and overclock (gt0/oc/vf_curve sysfs) for the
# Intel Arc Pro B60 / B70, on the running kernel.
#
# This is the VERIFIED recipe (Ubuntu 26.04 / kernel 7.0.0-27-generic, Arc B60).
# It deliberately does the two things that are easy to get wrong, correctly:
#   * builds from the FULL kernel source tree (xe tracepoints use a relative
#     TRACE_INCLUDE_PATH that only resolves in-tree — a `-C <headers> M=<xe>`
#     build FAILS on xe_trace.h), and
#   * takes the config + version from the running kernel's HEADERS package, so
#     vermagic and ABI come out correct automatically — NO .config editing and
#     NO vermagic forcing (doing either produces a module that loads but
#     silently won't bind the GPU → display falls back to simpledrm).
#
# Usage:
#   sudo bash scripts/build-xe-module.sh [--build-only] [--yes]
#
#   --build-only   build + verify, do NOT install (safe; leaves the running
#                  module untouched).
#   --yes          don't prompt before installing over the stock module.
#
# After a successful install you must REBOOT to activate (a live driver swap is
# unsafe while the desktop compositor holds the GPU). The script prints the
# post-reboot verification to run.
#
# NOTE: this installs a module into /lib/modules and it will persist across
# reboots, but a KERNEL UPDATE replaces it with the stock xe — see dkms/ for the
# auto-rebuild-on-update path, and docs/LINUX-BUILD.md for the manual walkthrough.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
KREL="$(uname -r)"
BASE_VER="${KREL%%-*}"                       # e.g. 7.0.0
HDR="/usr/src/linux-headers-${KREL}"
BUILD_ONLY=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --build-only) BUILD_ONLY=1 ;;
    --yes|-y)     ASSUME_YES=1 ;;
    *) echo "unknown arg: $a"; exit 2 ;;
  esac
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo (installs to /lib/modules, needs kernel headers)"
[ -d "$HDR" ] || die "kernel headers not found at $HDR — install them first (see below)."

# ---------------------------------------------------------------------------
log "1/8  Build prerequisites"
# The non-obvious ones: libdw-dev provides <dwarf.h> (gendwarfksyms), dwarves
# provides pahole (BTF). Distro-detect for the common package managers.
install_deps() {
  if command -v apt-get >/dev/null; then
    apt-get update -qq || true
    apt-get install -y build-essential flex bison libssl-dev libelf-dev \
                       libdw-dev dwarves "linux-source-${BASE_VER}" zstd \
      || die "apt dependency install failed"
  elif command -v pacman >/dev/null; then
    pacman -S --needed --noconfirm base-devel bc libelf dwarves zstd \
      || die "pacman dependency install failed"
    echo "NOTE: on Arch/CachyOS the kernel source comes from the *-headers pkg;"
    echo "      adjust SRC below if you are not on a Ubuntu-style linux-source layout."
  elif command -v dnf >/dev/null; then
    dnf install -y gcc make flex bison openssl-devel elfutils-libelf-devel \
                   elfutils-devel dwarves zstd "kernel-devel-${KREL}" \
      || die "dnf dependency install failed"
  else
    die "unsupported package manager; install: gcc make flex bison libssl-dev \
libelf-dev libdw-dev(dwarf.h) dwarves(pahole) + this kernel's source"
  fi
}
install_deps

# ---------------------------------------------------------------------------
log "2/8  Kernel source tree"
SRC="/usr/src/xe-arc-src/linux-source-${BASE_VER}"
TARBALL="$(ls /usr/src/linux-source-${BASE_VER}/linux-source-${BASE_VER}.tar.* 2>/dev/null | head -1 || true)"
[ -n "$TARBALL" ] || die "kernel source tarball not found under /usr/src/linux-source-${BASE_VER}/"
mkdir -p "$(dirname "$SRC")"
if [ ! -d "$SRC" ]; then
  echo "extracting $TARBALL ..."
  tar -xf "$TARBALL" -C "$(dirname "$SRC")"
fi
XE="$SRC/drivers/gpu/drm/xe"
[ -d "$XE" ] || die "xe driver not found in extracted source ($XE)"

# ---------------------------------------------------------------------------
log "3/8  Apply the fan patch"
FANPATCH="$(ls "$REPO_DIR"/patch/xe-fan-control-*.patch | head -1)"
[ -f "$FANPATCH" ] || die "fan patch not found in $REPO_DIR/patch/"
if ! grep -q 'pwm1_auto_point' "$XE/xe_hwmon.c"; then
  ( cd "$SRC" && patch -p1 --fuzz=5 -i "$FANPATCH" ) \
    || die "fan patch failed to apply to $BASE_VER — hand-port from the .patch"
else
  echo "fan patch already applied"
fi

# ---------------------------------------------------------------------------
log "4/8  Apply the overclock (VF-curve) feature"
# The impl files live in the repo (kernel/); the checked-in 0001-*.patch is a
# malformed skeleton — we copy the files and apply three anchored hooks instead,
# which is robust across kernel-version line shifts.
cp -v "$REPO_DIR/kernel/xe_gt_oc.c" "$REPO_DIR/kernel/xe_gt_oc.h" "$XE/"
python3 - "$XE" <<'PY'
import sys
XE = sys.argv[1]
# Makefile: add xe_gt_oc.o next to xe_gt_freq.o (copy that line's indentation)
mk = XE + "/Makefile"; lines = open(mk).read().split("\n")
if not any("xe_gt_oc.o" in l for l in lines):
    out = []
    for l in lines:
        out.append(l)
        if "xe_gt_freq.o" in l:
            indent = l[:len(l) - len(l.lstrip())]
            out.append(indent + "xe_gt_oc.o \\")
    open(mk, "w").write("\n".join(out)); print("  Makefile: +xe_gt_oc.o")
else:
    print("  Makefile: already present")
# xe_gt.c: include + init call, anchored on the xe_gt_freq equivalents
gt = XE + "/xe_gt.c"; s = open(gt).read()
if '#include "xe_gt_oc.h"' not in s:
    s = s.replace('#include "xe_gt_freq.h"\n',
                  '#include "xe_gt_freq.h"\n#include "xe_gt_oc.h"\n', 1)
    print("  xe_gt.c: +include")
if "xe_gt_oc_init(gt)" not in s:
    anchor = "\terr = xe_gt_freq_init(gt);\n\tif (err)\n\t\treturn err;\n"
    if anchor in s:
        s = s.replace(anchor,
                      anchor + "\n\terr = xe_gt_oc_init(gt);\n\tif (err)\n\t\treturn err;\n", 1)
        print("  xe_gt.c: +xe_gt_oc_init()")
    else:
        sys.exit("  !! xe_gt.c init anchor not found — hand-place xe_gt_oc_init(gt)")
open(gt, "w").write(s)
PY

# ---------------------------------------------------------------------------
log "5/8  Configure with the RUNNING kernel's real config (the key step)"
cp "$HDR/.config" "$SRC/.config"
# Clear the cert PATHS that break a from-source build (ABI-neutral: they only
# affect which keys are baked into vmlinux, not module struct layout).
( cd "$SRC" && ./scripts/config --set-str SYSTEM_TRUSTED_KEYS '' \
                                --set-str SYSTEM_REVOCATION_KEYS '' )
( cd "$SRC" && make olddefconfig >/dev/null )
( cd "$SRC" && make -j"$(nproc)" modules_prepare >/dev/null ) \
  || die "modules_prepare failed (missing libdw-dev / dwarves?)"

log "6/8  Pin version + symbols to the running kernel (no vermagic hacks)"
# The source Makefile's SUBLEVEL differs from the distro's release label, so take
# the generated version files + Module.symvers straight from the headers package.
cp "$HDR/include/generated/utsrelease.h" "$SRC/include/generated/utsrelease.h"
cp "$HDR/include/config/kernel.release"  "$SRC/include/config/kernel.release"
cp "$HDR/Module.symvers"                 "$SRC/Module.symvers"

# ---------------------------------------------------------------------------
log "7/8  Build the module"
( cd "$SRC" && make -C "$SRC" M=drivers/gpu/drm/xe clean >/dev/null 2>&1 || true )
( cd "$SRC" && env PATH="$PATH:$XE" make -j"$(nproc)" M=drivers/gpu/drm/xe modules ) \
  || die "module build failed"
[ -f "$XE/xe.ko" ] || die "xe.ko not produced"

BUILT_VM="$(modinfo "$XE/xe.ko" | awk '/^vermagic:/{print $2}')"
[ "$BUILT_VM" = "$KREL" ] \
  || die "vermagic mismatch: built '$BUILT_VM' != running '$KREL' (do NOT force it — the config/version pinning above is wrong)"
echo "  vermagic OK: $BUILT_VM"
# NB: use `grep -c` (reads all input), NOT `grep -q` — grep -q exits on first
# match, sending SIGPIPE upstream, and `set -o pipefail` then misreports these
# large pipes as failures even when the symbol IS present.
[ "$(nm "$XE/xe.ko" | grep -c xe_gt_oc_init)" -gt 0 ]        || die "xe_gt_oc_init missing from module"
[ "$(strings "$XE/xe.ko" | grep -c pwm1_auto_point1_pwm)" -gt 0 ] || die "fan sysfs missing from module"
echo "  fan + OC symbols present."

strip --strip-debug "$XE/xe.ko"
zstd -f -19 -T0 "$XE/xe.ko" -o "$XE/xe.ko.zst" >/dev/null

if [ "$BUILD_ONLY" -eq 1 ]; then
  log "Build-only: done. Module at $XE/xe.ko.zst (NOT installed)."
  exit 0
fi

# ---------------------------------------------------------------------------
log "8/8  Install"
MODDIR="/lib/modules/${KREL}/kernel/drivers/gpu/drm/xe"
STOCK="$MODDIR/xe.ko.zst"
BACKUP="/root/xe.ko.zst.stock-${KREL}"
if [ "$ASSUME_YES" -ne 1 ]; then
  echo "About to replace the in-tree xe with the patched build:"
  echo "  $STOCK"
  echo "  (backup -> $BACKUP)"
  read -r -p "Proceed? [y/N] " ans; [ "$ans" = y ] || { echo "aborted."; exit 0; }
fi
[ -f "$BACKUP" ] || cp "$STOCK" "$BACKUP"
cp "$XE/xe.ko.zst" "$STOCK"
depmod -a "$KREL"

cat <<EOF

\033[1;32mInstalled.\033[0m  REBOOT to activate, then verify the GPU actually binds:

  reboot
  # after reboot, from the box:
  lspci -k -s \$(lspci -Dn | awk '/8086:e2(11|23)/{print \$1; exit}') | grep 'in use'   # -> xe
  ls /sys/class/drm/card*/device/hwmon/hwmon*/pwm1_enable                              # fan
  find /sys/devices -path '*gt0/oc/vf_curve'                                           # OC

If the display comes up zoomed / low-res, the module didn't bind — restore the
stock module and reboot:
  cp $BACKUP $STOCK && depmod -a $KREL && reboot
EOF
