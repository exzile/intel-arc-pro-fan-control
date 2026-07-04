# Contributing to the Intel `xe` GPU driver — workflow

Two separate things: (A) the quick **Tested-by** on the existing fan series, and (B) the full
**git send-email** flow for submitting *new* patches (temp-limit, undervolt, etc.).

---

## A. Tested-by on the fan series (do this first — highest impact, minutes)

The kernel is developed on mailing lists, so a Tested-by is an **email reply** to the patch thread.

1. Find the thread: https://patchwork.freedesktop.org/series/168027/ → the cover letter
   `[PATCH v1 0/5] Add fan control support`. Or on lore:
   https://lore.kernel.org/intel-xe/?q=Add+fan+control+support
2. On lore, open the `[PATCH v1 0/5]` message → there's a `(raw)` / reply link, or click the
   message-id → your mail client can reply-to-all preserving threading. Easiest reliable path:
   in **any** mail client, reply-all to that message (To: the author + intel-xe@lists.freedesktop.org,
   keep the In-Reply-To/References headers) and paste the body from
   `contrib-mailinglist-testedby.txt`.
3. Send it as **plain text** (no HTML — kernel lists reject HTML mail).
4. Also post the GitHub confirmation: paste `contrib-885-comment.md` into
   https://github.com/intel/compute-runtime/issues/885 .

(If you don't want to wrestle with mail-client threading, the compute-runtime #885 GitHub comment
alone is still valuable — it's where Pro-card owners and Intel folks are watching.)

---

## B. Submitting a NEW kernel patch (temp-limit / undervolt / etc.)

### One-time setup
```
# 1. identity + send-email (App Password entered at send time, never stored)
./setup-git-sendemail.sh "Your Name" you@gmail.com

# 2. get the xe development tree (drm-xe-next is where xe patches land first)
git clone https://gitlab.freedesktop.org/drm/xe/kernel.git linux-xe
cd linux-xe
git checkout -b my-feature drm-xe-next     # base on the xe integration branch
# (mirror: https://cgit.freedesktop.org/drm/drm-xe/ ; or use drm-tip for latest)
```

### Make the change
- Edit `drivers/gpu/drm/xe/xe_hwmon.c` (+ `xe_pcode_api.h` for new PCODE opcodes).
- Add sysfs ABI docs in `Documentation/ABI/testing/sysfs-driver-intel-xe-hwmon` (REQUIRED for new
  hwmon nodes — reviewers will bounce a patch without them). The fan series 168027 is the template.
- Match kernel style. Before committing, run the checkers:
```
scripts/checkpatch.pl --strict <yourfile>     # or on the patch after format-patch
```

### Commit with a proper message + sign-off
```
git commit -s        # -s adds Signed-off-by: Your Name <you@gmail.com> (DCO — required)
```
Message format: `drm/xe/hwmon: <what>` subject line, body explaining *why*, wrap at ~72 cols.

### Generate + check the patch(es)
```
git format-patch -o /tmp/patches drm-xe-next --cover-letter   # cover letter for a multi-patch series
scripts/checkpatch.pl /tmp/patches/*.patch                    # fix warnings before sending
# get the exact people to CC:
scripts/get_maintainer.pl /tmp/patches/0001-*.patch
```
Typical CC for xe: `intel-xe@lists.freedesktop.org` (the list) + the maintainers get_maintainer
prints (Rodrigo Vivi, Lucas De Marchi, Thomas Hellström), plus for hwmon changes the hwmon
maintainer + `linux-hwmon@vger.kernel.org`.

### Send it
```
git send-email --to=intel-xe@lists.freedesktop.org \
  --cc="$(scripts/get_maintainer.pl --no-rolestats /tmp/patches/0001-*.patch | paste -sd, -)" \
  /tmp/patches/*.patch
```
- With `sendemail.confirm=always` + `annotate=yes` (set by the setup script) you review each mail
  and the recipient list before anything leaves.
- For a v2 after review: `git send-email --in-reply-to=<msgid-of-v1-cover> -v2 ...` to thread it.

### Etiquette
- **RFC first for undervolt/voltage** — post as `[RFC PATCH]` to agree the sysfs interface before
  writing the full thing; voltage control is sensitive and maintainers will want a design.
- Expect review rounds; respond to each comment inline, resend as v2/v3.
- Never send HTML mail; never top-post on the list.

---

## Files in this dir
- `contrib-885-comment.md` — ready to paste into compute-runtime issue #885.
- `contrib-mailinglist-testedby.txt` — ready to send as the Tested-by reply (fill in your name/email).
- `setup-git-sendemail.sh` — one-time git identity + send-email config.
- (kernel tree + your new patches: created when you do part B.)
