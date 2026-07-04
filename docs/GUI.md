# Desktop GUI — `xe-gpu-gui`

A native **GTK4 / libadwaita** control panel for the Arc (xe) GPU. It replaces the need for a
third-party tool like CoolerControl — the fan-curve editor is built in.

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
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

All writes go through the `xe-fan-curve` / `xe-gpu-tune` helpers with `pkexec`, so you get a normal
authorization prompt and nothing runs elevated in the background.
