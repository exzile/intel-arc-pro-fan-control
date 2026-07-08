# Distribution & Portability Roadmap

How to make the fan-control / overclock kernel support install and work on
**arbitrary users' machines and kernels**, not just the box it was developed on.

> **Status:** roadmap / design. The userland side (GUI + CLI + `xe-gpu-vram`
> service via `install.sh`) is already portable and distro-agnostic. This
> document is about the hard part: the **patched `xe` kernel module**.

---

## The core constraint (read this first)

Fan control and the VF-curve overclock are implemented by **patching the
in-tree `xe` driver** (new sysfs in `xe_hwmon.c`, a new `xe_gt_oc.c`, plus small
hooks in `xe_gt.c` / the `xe` `Makefile`). That has an unavoidable consequence:

* **There is no single prebuilt `.ko` that runs on all kernels.** A kernel
  module must match the running kernel's version **and its build config** — the
  `vermagic` string *and* the ABI (struct layouts, which depend on `CONFIG_*`).
* The patch must also **apply to that kernel's `xe` source**, and `xe` changes
  every kernel release.

So the goal is not "one binary for everyone." The goal is: **automatically build
the correct module for each user's kernel, and fail loudly (not silently) when a
kernel isn't supported.**

### Hard-won lesson: never hand-massage the kernel config

A module that is built against a **mismatched `.config`** will often *load*
(if you force the `vermagic`) but then **silently fail to bind the GPU** — no
dmesg error, the display just falls back to `simpledrm` at low resolution. This
happened during development when the module was built from a freshly
*re-configured* `linux-source` tree (edited `.config`, `olddefconfig`, forced
`utsrelease`). The fix is to **never touch the config** — build against the
kernel's *real* config, which is exactly what the headers package and DKMS
provide. If you ever find yourself force-overriding `vermagic`, stop: your build
environment doesn't match the kernel, and the ABI is probably wrong too.

---

## Tier 1 — DKMS packaging (do this first; biggest impact)

[DKMS](https://github.com/dell/dkms) is the standard mechanism for shipping
out-of-tree modules (`nvidia-dkms`, `zfs-dkms`, `v4l2loopback`, …). You register
the source once; DKMS then **rebuilds the module against each machine's own
installed kernel headers** — its exact `.config` and `Module.symvers` — on
install **and automatically on every kernel update**.

This eliminates the entire class of failure above, for every user, forever:

* ✅ `vermagic` is correct automatically (no override)
* ✅ config matches exactly (no ABI drift)
* ✅ symbol CRCs match (no copying `Module.symvers`)
* ✅ survives kernel upgrades (auto-rebuild hook)

### Sketch

Ship the patched `xe` source as a DKMS module tree, e.g. `xe-arc/<version>/`:

```
/usr/src/xe-arc-<version>/
  dkms.conf
  <patched xe/ source: all .c/.h + Makefile>
  patch/         # the fan + OC changes, applied at build time if shipping pristine + patch
```

`dkms.conf` (illustrative):

```ini
PACKAGE_NAME="xe-arc"
PACKAGE_VERSION="<version>"
MAKE[0]="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build modules"
CLEAN="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build clean"
BUILT_MODULE_NAME[0]="xe"
DEST_MODULE_LOCATION[0]="/updates"      # loaded in preference to the stock in-tree xe
AUTOINSTALL="yes"
```

Key points / caveats:
* `${kernel_source_dir}` resolves to `/lib/modules/$(uname -r)/build` — the
  **headers package**, i.e. the kernel's real config. This is the whole point.
* We are **replacing** the in-tree `xe`, so `DEST_MODULE_LOCATION=/updates`
  (modules there win over `kernel/drivers/gpu/drm/xe`). Document that clearly —
  this is more invasive than a normal add-on module.
* Because it's the display driver, a bad build must not brick boot. Keep the
  "verify it binds" gate (below) and a documented recovery (`dkms remove` +
  regenerate initramfs, or boot the stock module).
* User prerequisites: `dkms` + `linux-headers-$(uname -r)` (one apt line).

### The "verify it binds" gate (mandatory)

Tonight's failure loaded but didn't bind, with no error. Any installer/DKMS
post-build step should **actually confirm the GPU binds** before declaring
success — e.g. after install, check `lspci -k -s <bdf>` shows
`Kernel driver in use: xe` and `/dev/dri/renderD*` exists, and that the display
is **not** on `simple-framebuffer`. If not, roll back to the stock module.

---

## Tier 2 — Make the patch survive across kernel versions

DKMS guarantees a *correct* build; it does **not** guarantee the *patch applies*.
`xe` source shifts between releases, so a single patch won't apply everywhere.

* **Prefer new files + minimal hooks.** The OC feature already does this well:
  `xe_gt_oc.{c,h}` are new (version-independent); only three tiny hooks touch
  existing files (`Makefile` += `xe_gt_oc.o`; `xe_gt.c` include + one init call).
  Refactor the **fan** side the same way where feasible so less of it depends on
  exact `xe_hwmon.c` context.
* **Anchor-based hook applier**, not line numbers. Search for a code anchor
  (e.g. the `xe_gt_freq_init(gt)` block) and insert relative to it, so small
  upstream shifts don't break application. (This is how the hooks were applied
  by hand during development — codify it.)
* **A small set of per-major-version patches** selected by `uname -r` where the
  additions genuinely differ, rather than one brittle patch.
* **Supported-kernel matrix** + **graceful failure**: detect the kernel, and if
  it's outside the tested set, print a clear "unsupported kernel X; fan/OC will
  not be enabled" message instead of building something that won't bind.

> Note: the checked-in `patch/0001-drm-xe-add-vf-curve-overclocking-sysfs.patch`
> is a **malformed skeleton** (placeholder index hashes, wrong `@@` hunk counts —
> `patch` rejects it as "malformed"). The real implementation lives in
> `kernel/xe_gt_oc.{c,h}`. **Fix or regenerate that patch**, or replace the
> patch-based OC flow with "copy `kernel/xe_gt_oc.*` + apply the three hooks."

---

## Tier 3 — Multi-distro support

Header/source packages differ per distro; DKMS itself is distro-agnostic. A thin
detect layer in the installer covers the big ones:

| Distro | Headers package |
|---|---|
| Ubuntu / Debian | `linux-headers-$(uname -r)` |
| Arch / CachyOS | `linux-headers` (per-kernel: `linux-cachyos-headers`, …) |
| Fedora | `kernel-devel` (matching `uname -r`) |
| openSUSE | `kernel-default-devel` |

Also required: `build-essential`/`base-devel`, and the non-obvious build deps
that bit us — **`libdw-dev`** (provides `dwarf.h` for `gendwarfksyms`) and
**`dwarves`** (`pahole`, for BTF). Document these per distro.

---

## Tier 4 — The two "real" long-term answers

These remove the fragility instead of managing it.

### 4a. Decouple from `xe` (most portable)

Fan control ultimately writes the GPU's **PCODE mailbox (opcode `0x7d`,
`FAN_SPEED_CONTROL`)** — the same operation the Windows app performs via
IGCL/escape. A **standalone module** (or a userspace helper using the GPU's MMIO
BAR / PMT) that pokes the mailbox **without patching `xe`** would run on **any**
kernel that loads `xe`, with **no rebuild ever**. Significantly more work
(reimplement mailbox access + forcewake outside `xe`), but it is the
architecture that actually scales to arbitrary users. See
[GPU-TUNING.md](GPU-TUNING.md) / the register + opcode notes.

### 4b. Upstream it

The fan-control changes derive from **Intel patch series 168027**. If that lands
in mainline `xe`, every sufficiently recent kernel exposes fan control
**natively** — zero patching for those users. Best possible outcome; worth
tracking/pushing that series and pointing users on new kernels at the native
sysfs.

---

## Recommended sequencing

1. **Get one known-good build** on a reference machine using the **headers
   method** (`make -C /usr/src/linux-headers-$(uname -r) M=<patched-xe> modules`)
   — no config edits, no `vermagic` forcing. Confirm the GPU **binds** and the
   `pwm1_*` + `gt0/oc/vf_curve` sysfs appear.
2. **Wrap it in DKMS** → instantly works-and-auto-rebuilds for everyone, on every
   kernel update. (~80% of the "works for many people" goal.)
3. **Harden the patch** toward new-files-plus-anchored-hooks + a supported-kernel
   list + the "verify it binds" gate. Fix the malformed OC patch.
4. **Package** for the target distros (`.deb` with `dkms` trigger first, then
   AUR/COPR as demand appears).
5. **Longer term:** evaluate the standalone-PCODE-module approach (4a) and track
   upstreaming (4b) for true universality.

---

## Known issues to fix along the way

* [ ] `patch/0001-drm-xe-add-vf-curve-overclocking-sysfs.patch` is malformed —
      regenerate or replace with a copy-files + anchored-hooks flow.
* [ ] `apply_xefan.sh` covers only the fan patch and assumes a prepared source
      tree; it should move to the headers-based build and add the OC step.
* [ ] Build prerequisites are undocumented — at minimum: `build-essential`
      (or distro equivalent), `libdw-dev` (`dwarf.h`), `dwarves` (`pahole`), and
      the matching kernel headers.
* [ ] No "did the module actually bind the GPU?" verification after install —
      add it so a bad build can never leave a user on `simpledrm` unknowingly.
* [ ] Document the recovery path (restore stock `xe`, rebuild initramfs) plainly.
