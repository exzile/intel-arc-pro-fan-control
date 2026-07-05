# Desktop GUI — `xe-gpu-gui`

A native **GTK4 / libadwaita** control panel for the Arc (xe) GPU. It replaces the need for a
third-party tool like CoolerControl — the fan-curve editor is built in.

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
sudo install -m755 gui/xe-gpu-gui.py /usr/local/bin/xe-gpu-gui
install -m644 gui/xe-gpu-gui.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```
Launch **Arc GPU Dashboard** from your apps menu, or run `xe-gpu-gui`.

## Dashboard tab
Live (2 s) readout of clocks (with a frequency bar), power cap + I1 crit, fan RPM/duty/mode, and every
temperature sensor — the four mains (pkg / mctrl / pcie / vram) plus all VRAM channels, colour-coded by
headroom to each sensor's crit limit, with a flame on the hottest. Controls:

- **Fan**: *Curve* (opens the editor), *Auto* (stock table), *Max* (full speed).
- **Power cap / Min clock / Max clock** spinners → **Apply** / **Reset**. Hover the ⓘ icons for what
  each does (min clock is the idle floor — lowering it saves idle power/heat).

## Fan Curve tab
A graphical editor for the 10-point hardware fan table:

- **Drag** the points to shape the curve (X = GPU temp °C, Y = fan speed %).
- **Right-click** a point to remove it; **＋ Point** adds one at the widest gap (up to 10).
- **Preset…** loads Silent / Balanced / Cool profiles; **Reload** reads the curve currently on the card.
- The dashed vertical line shows the current package temperature.
- **Apply** writes it as the manual curve (via `xe-fan-curve set …`, prompted by polkit).

## Overclock tab

Appears only when the `xe_gt_oc` patch is loaded (it exposes `.../gt0/oc/vf_curve` — see
[OVERCLOCKING.md](OVERCLOCKING.md)). It shows the GPU's **voltage-frequency curve** as a graph
(X = frequency step, idle → max; Y = voltage in mV): stock is dashed, your preview is the solid
accent line with a shaded fill, and the red dashed line marks your voltage limit.

The curve is **colour-zoned** by how far each region sits from stock: green where you've
undervolted, accent-blue near stock, amber → red as you overvolt (the anchor nodes and the shaded
fill take the same colour). The voltage-offset label turns green (undervolt) or amber (overvolt),
and the temp-limit label turns red when raised to 95 °C+.

**Preset profiles** — the *Preset…* dropdown (top-right) loads a conservative profile into the
sliders (offset mode): **Stock**, **Efficient** (−50 mV undervolt, cool/quiet, 85 °C cap),
**Balanced** (−25 mV), **Performance** (+25 mV, 20 Gbps VRAM). Nothing is written until you press
Apply, so a preset is a safe starting point you can then fine-tune.

**Two adjustment modes** (the checkbox at the top switches between them):

- **Offset** (default) — the **Voltage offset** slider shifts the *whole* curve uniformly. Drag left
  to **undervolt** (−mV: cooler, more efficient) or right to **overvolt** (+mV: headroom for higher
  clocks). The graph previews the shifted curve live.
- **Per-point curve** — tick *“Per-point curve (drag the nodes)”* and the graph grows draggable
  **anchor nodes**. Drag a node up/down to set the voltage for that frequency region independently
  (e.g. undervolt hard at high clocks, leave idle alone). The full 85-point curve is written in one
  transaction.

Every knob is an **aligned slider + number box** (they stay in sync), with an icon and a live
accent highlight when it differs from what's on the card:

- **Voltage offset** — see above (offset mode only).
- **Voltage limit** — a ceiling on the curve's peak voltage; the applied curve (and the graph) is
  clamped here. A safety cap on how high voltage can go.
- **Power limit** — board power cap (TDP) in watts (via `xe-gpu-tune`).
- **Memory speed** — GDDR6 data rate in Gbps (a separate VRAM overclock).
- **Temp limit** — the GPU thermal-throttle target (°C); raise it for more sustained clock, lower
  it to run cooler/quieter.

- **Apply** (it pulses when there are pending changes) writes only what changed
  (`xe-gpu-oc offset … / curve … / mem … / temp …`, `xe-gpu-tune …`); **Reset** restores stock
  curve + memory + temp; **Reload** re-reads from the GPU.
- **Test** runs a **stability check**: a 60 s GPU load (`xe-gpu-stress`, vsync uncapped) while it
  watches clocks + package temp and the kernel log for a GPU hang/reset. It reports **Stable**,
  **Throttled** (peak hit your temp limit — stable but thermally capped), or **Unstable** (a hang
  or crash under load) — and on Unstable it **auto-reverts to stock**. Needs a workload installed
  (`sudo apt install glmark2` — or `vkmark`; `glxgears` works as a light fallback). Verify a new
  overclock with this before you rely on it.
- Voltage is clamped to a safe 400–1200 mV. The curve is kept **monotonic** (voltage rises with
  frequency) to match what PCODE will accept — dragging a node below its neighbour pins it up rather
  than silently failing, and the top points sit on the fixed Vmax rail. So the preview is exactly
  what lands.

All writes go through the `xe-fan-curve` / `xe-gpu-tune` / `xe-gpu-oc` helpers with `pkexec`, so you
get a normal authorization prompt and nothing runs elevated in the background.
