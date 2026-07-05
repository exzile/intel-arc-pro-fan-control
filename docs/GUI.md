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
(X = frequency step, idle → max; Y = voltage in mV), with the stock curve dashed and your preview
solid.

- **Voltage offset** slider — drag left to **undervolt** (−mV: cooler, more efficient) or right to
  **overvolt** (+mV: headroom for higher clocks). The graph previews the shifted curve live.
- **Memory speed** — sets the GDDR6 data rate in Gbps (a separate VRAM overclock).
- **Apply** writes whatever changed (`xe-gpu-oc offset …` / `mem …`); **Reset** restores the stock
  curve + memory speed; **Reload** re-reads from the GPU.
- Voltage is clamped to a safe 400–1200 mV. Combine with the Dashboard's power/clock controls for a
  full overclock.

All writes go through the `xe-fan-curve` / `xe-gpu-tune` / `xe-gpu-oc` helpers with `pkexec`, so you
get a normal authorization prompt and nothing runs elevated in the background.
