# Submitting the VF-curve OC patch upstream (drm/xe)

The `xe_gt_oc` patch (`kernel/xe_gt_oc.c`, `kernel/xe_gt_oc.h`, `patch/0001-…`)
is ready for the `intel-xe` list. Because a mainline submission carries **your**
DCO sign-off and goes out under **your** email to a public list, the final send
is yours to run — everything below is the exact recipe.

## 1. Get a current xe tree

```bash
git clone https://gitlab.freedesktop.org/drm/xe/kernel.git xe-kernel
cd xe-kernel
```

## 2. Apply the change to that tree

The patch in `patch/` was authored against Ubuntu 7.0.0; regenerate it against
the current tree so the hunk offsets match:

```bash
# copy the two source files in
cp /path/to/repo/kernel/xe_gt_oc.c drivers/gpu/drm/xe/
cp /path/to/repo/kernel/xe_gt_oc.h drivers/gpu/drm/xe/

# wire the Makefile + xe_gt.c (same two lines the installer adds)
sed -i 's|\txe_gt_freq.o \\|\txe_gt_freq.o \\\n\txe_gt_oc.o \\|' drivers/gpu/drm/xe/Makefile
sed -i 's|#include "xe_gt_freq.h"|#include "xe_gt_freq.h"\n#include "xe_gt_oc.h"|' drivers/gpu/drm/xe/xe_gt.c
# add the xe_gt_oc_init(gt) call after xe_gt_freq_init(gt) in xe_gt_init()
$EDITOR drivers/gpu/drm/xe/xe_gt.c
```

## 3. Build + checkpatch

```bash
make -j"$(nproc)" M=drivers/gpu/drm/xe            # must compile clean
./scripts/checkpatch.pl --strict --git HEAD       # after committing, below
```

## 4. Commit with YOUR sign-off (the DCO attestation)

```bash
git add drivers/gpu/drm/xe/xe_gt_oc.c drivers/gpu/drm/xe/xe_gt_oc.h \
        drivers/gpu/drm/xe/Makefile drivers/gpu/drm/xe/xe_gt.c
git commit -s        # -s adds your Signed-off-by (you are attesting the DCO)
```

Paste the commit message from `patch/0001-drm-xe-add-vf-curve-overclocking-sysfs.patch`
(the text above the `---`). Keep the "derived by tracing … verified on a real B60"
paragraph — maintainers will want to know provenance and testing.

## 5. Format + get the recipients

```bash
git format-patch -1 -o outgoing/
./scripts/get_maintainer.pl outgoing/0001-*.patch
```

Primary recipients for drm/xe are the list plus the maintainers:
`intel-xe@lists.freedesktop.org`, and (verify with get_maintainer) the xe
maintainers/reviewers. CC `dri-devel@lists.freedesktop.org`.

## 6. Send (one command — yours to run)

```bash
# one-time: scripts/setup-git-sendemail.sh configures your SMTP
git send-email \
  --to='intel-xe@lists.freedesktop.org' \
  --cc='dri-devel@lists.freedesktop.org' \
  outgoing/0001-*.patch
```

## Likely review feedback (worth pre-empting)

- **Voltage bounds / units.** Maintainers may want the mV encoding and the
  400–1200 clamp documented or sourced from PCODE rather than hard-coded.
- **ABI documentation.** Add a `Documentation/ABI/testing/sysfs-driver-xe-oc`
  entry describing `oc/vf_curve`.
- **Locking.** The transaction issues ~87 back-to-back PCODE calls, each taking
  `tile->pcode.lock` individually; a reviewer may ask to hold the mailbox across
  the begin→…→end so nothing interleaves. That needs a locked pcode helper.
- **Scope.** Keep it minimal (VF curve only). Power/clock already have sysfs;
  don't bundle them.
