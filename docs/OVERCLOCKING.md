# Overclocking the Arc Pro B60/B70 on Linux

The stock `xe` driver exposes power (`power1_cap`) and frequency (`freq0/max_freq`)
controls, but **not** the GPU's voltage-frequency (VF) curve — so undervolting /
overvolting was impossible on Linux even though the hardware supports it exactly as
it does on Windows. This repo adds the missing piece: an `xe_gt_oc` patch that
exposes the VF curve, plus the `xe-gpu-oc` CLI on top of it.

## What it does

The GPU holds an **85-point voltage-frequency curve** — each point is a frequency
step whose value is the voltage (mV). Lowering the curve undervolts (cooler / more
efficient, or higher sustained clocks under a power cap); raising it gives headroom
for higher clocks.

The `xe_gt_oc` patch adds a sysfs attribute:

```
/sys/bus/pci/devices/<bdf>/tile0/gt0/oc/vf_curve      # read/write
```

- **read**: one `<index> <voltage_mV>` line per point
- **write**: one or more `<index> <voltage_mV>` lines; unlisted points keep their
  value; voltage is clamped to a safe 400–1200 mV window
- runs under a runtime-PM reference, so it works even while the GPU is autosuspended

### Why the stock driver lacks it

Writing the curve is a PCODE *transaction* — a `begin` command, 85 point writes, an
`end` — and stock `xe` never issues the `begin`, so PCODE rejects the point writes
outright. The vendor driver does issue it; this patch replicates that exact
sequence (see the patch commit message and `docs/EVIDENCE.md` for how it was
derived and verified). The LATE_BINDING capability and per-point reads are byte-for-byte
identical between Linux and Windows — the transaction was the only gap.

## Install

The VF-curve control needs the small `xe_gt_oc` kernel patch:

```bash
sudo bash scripts/apply_xeoc.sh      # builds + installs the OC-enabled xe.ko
sudo reboot                          # xe is in use; reboot to load it
```

This only *adds* one source file plus two one-line wires, so it composes cleanly
with the fan-control build (`apply_xefan.sh`) — run both for fan + OC.

## Use

```bash
sudo xe-gpu oc read              # dump the curve (index -> voltage mV)
sudo xe-gpu oc offset -25        # undervolt every point by 25 mV
sudo xe-gpu oc offset 30         # overvolt (headroom for higher clocks)
sudo xe-gpu oc set 60 980        # set one point (index 60) to 980 mV
sudo xe-gpu oc reset             # restore the saved stock curve
```

The first change saves the stock curve to `/var/lib/xe-gpu-oc/stock-curve`, so
`reset` always brings you back. `offset` is **absolute from stock** (not cumulative),
so `offset -25` always means "stock minus 25 mV" no matter what's currently applied.

## Persistence across reboots

The GPU **forgets** the curve on every cold boot — the firmware re-provisions stock.
So your choice is saved to `/etc/xe-gpu-oc.conf` (`VOLTAGE_OFFSET=…`) and re-applied at
boot by a systemd unit:

```bash
sudo cp systemd/xe-gpu-oc.service /etc/systemd/system/
sudo cp systemd/etc/xe-gpu-oc.conf /etc/            # template (managed for you)
sudo systemctl daemon-reload
sudo systemctl enable --now xe-gpu-oc.service       # re-applies VOLTAGE_OFFSET at boot
```

Every `xe-gpu-oc offset` / `reset` (and the GUI's Apply/Reset) rewrites the config, so
whatever you last chose comes back automatically after a reboot. The GUI's Overclock tab
also reads the saved offset on launch, so it opens showing your current setting rather
than resetting to zero. `xe-gpu-oc status` prints the persisted value.

### A complete overclock

Combine the VF curve with the existing power/clock knobs (`xe-gpu-tune`):

```bash
sudo xe-gpu tune --power 120     # raise the power limit (headroom to boost)
sudo xe-gpu tune --max 2600      # raise the max frequency
sudo xe-gpu oc offset 25         # give the curve a little more voltage for stability
```

Then stress-test and watch temps/clocks:

```bash
sudo xe-gpu watch                # live dashboard
```

## Safety

- Voltage is clamped to **400–1200 mV**. Start small; undervolts of −20…−40 mV are
  usually safe and beneficial. Overvolting raises heat and power draw.
- An unstable curve can hang the GPU; `reset` (or a reboot — settings are not
  persistent across boot unless you re-apply them) restores stock.
- These are **your** hardware and warranty. Overclocking is at your own risk.
