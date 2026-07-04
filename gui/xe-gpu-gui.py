#!/usr/bin/env python3
# xe-gpu-gui — native GTK4/libadwaita control panel for the Intel Arc (xe) GPU.
# Tabs: Dashboard (live stats + tuning) and Fan Curve (graphical draggable editor).
# Controls call xe-fan-curve / xe-gpu-tune via pkexec (polkit prompts for writes).
# Reads are unprivileged sysfs; no kernel poking here.
import os, glob, subprocess, threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk  # noqa: E402

REFRESH_SECONDS = 2
TMIN, TMAX = 20, 100          # curve editor temperature axis (°C)
MAX_POINTS = 10

CSS = b"""
.card2 { background: @card_bg_color; border-radius: 12px; padding: 12px 14px; }
.section { font-weight: bold; opacity: 0.7; font-size: 0.80em; letter-spacing:.04em; }
.big { font-size: 1.7em; font-weight: 800; }
.dim { opacity: 0.60; font-size: 0.86em; }
.chip { border-radius: 9px; padding: 7px 6px; background: alpha(@window_fg_color,0.05); }
.chip .cval { font-weight: 700; }
.chip .clbl { opacity: 0.60; font-size: 0.74em; }
.t-cool { color:#3584e4; } .chip.t-cool{ background:alpha(#3584e4,0.12);}
.t-warm { color:#e5a50a; } .chip.t-warm{ background:alpha(#e5a50a,0.16);}
.t-hot  { color:#e01b24; } .chip.t-hot { background:alpha(#e01b24,0.18);}
.info { opacity:0.45; }
"""


# ---------------------------------------------------------------- data
def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return None


def _int(p):
    v = _read(p)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class XeGpu:
    def __init__(self):
        self.card = self.hwmon = None
        for c in sorted(glob.glob("/sys/class/drm/card*")):
            drv = os.path.join(c, "device", "driver")
            if os.path.islink(drv) and os.path.basename(os.path.realpath(drv)) == "xe":
                self.card = c
                break
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if _read(os.path.join(d, "name")) == "xe":
                self.hwmon = d
                break

    @property
    def present(self):
        return bool(self.hwmon or self.card)

    def identity(self):
        if not self.card:
            return {"card": "-", "pci": "-", "id": "-"}
        pci = os.path.basename(os.path.realpath(os.path.join(self.card, "device")))
        did = (_read(os.path.join(self.card, "device", "device")) or "").replace("0x", "")
        return {"card": os.path.basename(self.card), "pci": pci, "id": "8086:" + did}

    def clocks(self):
        gt = os.path.join(self.card or "", "device/tile0/gt0/freq0")
        g = lambda n: _int(os.path.join(gt, n))  # noqa: E731
        return {"cur": g("cur_freq"), "min": g("min_freq"), "max": g("max_freq"),
                "rpn": g("rpn_freq"), "rp0": g("rp0_freq")}

    def power(self):
        if not self.hwmon:
            return {}
        cap = _int(os.path.join(self.hwmon, "power1_cap"))
        crit = _int(os.path.join(self.hwmon, "power1_crit"))
        return {"cap_w": (cap // 1_000_000) if cap else None,
                "crit_w": (crit / 1_000_000) if crit else None}

    def fan(self):
        if not self.hwmon:
            return {}
        pwm = _int(os.path.join(self.hwmon, "pwm1_enable"))
        return {"rpm": _int(os.path.join(self.hwmon, "fan1_input")),
                "max": _int(os.path.join(self.hwmon, "fan1_max")),
                "duty": _int(os.path.join(self.hwmon, "pwm1")),
                "mode": {0: "full", 1: "manual curve", 2: "auto"}.get(pwm, "?")}

    def tmap(self):
        # cache (label, input_path, crit_c) once — labels/crit are static
        if getattr(self, "_tmap_cache", None) is None:
            m = []
            for f in sorted(glob.glob(os.path.join(self.hwmon or "_", "temp*_input"))):
                base = os.path.join(self.hwmon, "temp" + os.path.basename(f)[4:-6])
                crit = _int(base + "_crit")
                m.append((_read(base + "_label") or os.path.basename(base), f,
                          (crit // 1000) if crit else None))
            self._tmap_cache = m
        return self._tmap_cache

    def temps_where(self, channel):
        # channel=False -> pkg/mctrl/pcie/vram (fast); True -> vram_ch_* (slow, ~100ms each)
        out = []
        for lbl, f, crit in self.tmap():
            if lbl.startswith("vram_ch_") != channel:
                continue
            v = _int(f)
            if v is not None:
                out.append({"label": lbl, "c": v // 1000, "crit": crit})
        return out

    def temps(self):
        return self.temps_where(False) + self.temps_where(True)

    def pkg_temp(self):
        for lbl, f, crit in self.tmap():
            if lbl == "pkg":
                v = _int(f)
                return v // 1000 if v is not None else None
        return None

    def snapshot(self):
        # one full read of everything — call this OFF the main thread
        return {"id": self.identity(), "clocks": self.clocks(), "power": self.power(),
                "fan": self.fan(), "mains": self.temps_where(False),
                "vram": self.temps_where(True)}

    def read_curve(self):
        pts = []
        for i in range(1, MAX_POINTS + 1):
            t = _int(os.path.join(self.hwmon or "_", f"pwm1_auto_point{i}_temp"))
            p = _int(os.path.join(self.hwmon or "_", f"pwm1_auto_point{i}_pwm"))
            if t is None:
                break
            pts.append([t // 1000, p if p is not None else 0])
        # collapse a flat/degenerate stock table to a sensible default
        temps = [t for t, _ in pts]
        if not pts or len(set(p for _, p in pts)) <= 1 or len(set(temps)) < 3:
            return list(PRESETS["Balanced"])
        return pts


PRESETS = {
    "Silent":     [[40, 0], [55, 45], [70, 100], [82, 170], [90, 255]],
    "Balanced":   [[40, 60], [55, 95], [65, 140], [75, 195], [85, 255]],
    "Cool/Loud":  [[35, 90], [50, 150], [65, 205], [80, 255]],
}


def tclass(c, crit):
    if crit and c >= crit - 10:
        return "t-hot"
    if crit and c >= crit - 25:
        return "t-warm"
    return "t-cool"


HELPER_PATHS = {"xe-fan-curve": "/usr/local/bin/xe-fan-curve",
                "xe-gpu-tune": "/usr/local/bin/xe-gpu-tune"}


def run_priv(args, parent, after=None):
    # pkexec sanitizes PATH (often no /usr/local/bin) -> use absolute paths.
    args = [HELPER_PATHS.get(args[0], args[0])] + list(args[1:])
    try:
        p = subprocess.Popen(["pkexec"] + args, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        parent.toast(f"Could not run: {e}")
        return

    def check():
        rc = p.poll()
        if rc is None:
            return True  # still running / awaiting authorization
        if rc == 0:
            if after:
                after()
        elif rc == 126:   # pkexec: authorization dismissed / not authorized
            parent.toast("Authorization cancelled")
        else:
            err = (p.stderr.read() or "").strip().splitlines()
            parent.toast("Failed: " + (err[-1] if err else f"exit {rc}"))
        return False
    GLib.timeout_add(250, check)


# ---------------------------------------------------------------- small widgets
def card(title, info=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.add_css_class("card2")
    head = Gtk.Box(spacing=6)
    lbl = Gtk.Label(label=title.upper(), xalign=0, hexpand=True)
    lbl.add_css_class("section")
    head.append(lbl)
    if info:
        head.append(info_icon(info))
    box.append(head)
    return box


def info_icon(text):
    img = Gtk.Image(icon_name="help-about-symbolic")
    img.add_css_class("info")
    img.set_tooltip_text(text)
    return img


def kv(grid, row, key, val_widget, tip=None):
    kb = Gtk.Box(spacing=5)
    k = Gtk.Label(label=key, xalign=0)
    k.add_css_class("dim")
    kb.append(k)
    if tip:
        kb.append(info_icon(tip))
    grid.attach(kb, 0, row, 1, 1)
    grid.attach(val_widget, 1, row, 1, 1)
    return val_widget


def icon_button(icon, tooltip, cb, label=None, css=None):
    b = Gtk.Button(tooltip_text=tooltip)
    if label:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=label))
    else:
        b.set_icon_name(icon)
    if css:
        b.add_css_class(css)
    b.connect("clicked", cb)
    return b


# ---------------------------------------------------------------- fan curve editor
class CurveEditor(Gtk.Box):
    def __init__(self, gpu, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.gpu = gpu
        self.window = window
        self.points = gpu.read_curve()
        self.applied = [list(p) for p in self.points]   # what's currently on the GPU
        self.cur_pkg = gpu.pkg_temp()   # cached; updated by the window refresh (no per-frame read)
        self._drag = None
        self._moved = False

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_size_request(520, 300)
        self.area.set_draw_func(self._draw)
        frame = Gtk.Frame()
        frame.add_css_class("card2")
        frame.set_child(self.area)
        self.append(frame)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_begin)
        drag.connect("drag-update", self._on_update)
        drag.connect("drag-end", self._on_end)
        self.area.add_controller(drag)
        rc = Gtk.GestureClick(button=3)
        rc.connect("released", self._on_right)
        self.area.add_controller(rc)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.area.add_controller(motion)
        self._cursor = None

        # toolbar
        bar = Gtk.Box(spacing=8)
        self.preset = Gtk.DropDown.new_from_strings(["Preset…"] + list(PRESETS))
        self.preset.set_tooltip_text("Load a preset fan curve")
        self.preset.connect("notify::selected", self._on_preset)
        bar.append(self.preset)
        bar.append(icon_button("list-add-symbolic", "Add a curve point", self._add, label="Point"))
        bar.append(icon_button("view-refresh-symbolic", "Reload the curve currently on the GPU",
                               lambda *_: (self._reload()), label="Reload"))
        bar.append(icon_button("edit-undo-symbolic",
                               "Revert the fan to the card's stock auto table (undo manual control)",
                               self._stock, label="Stock"))
        self.hint = Gtk.Label(xalign=0, hexpand=True)
        self.hint.add_css_class("dim")
        bar.append(self.hint)
        self.apply_btn = icon_button("emblem-ok-symbolic",
                                     "Apply this curve (manual mode) — asks for authorization",
                                     self._apply, label="Apply", css="suggested-action")
        bar.append(self.apply_btn)
        self.append(bar)
        note = Gtk.Label(
            label="Drag points to shape the curve · right-click a point to remove · "
                  "X = GPU temp °C, Y = fan speed %. The dashed line is the current package temp.",
            xalign=0, wrap=True)
        note.add_css_class("dim")
        self.append(note)
        self._mark()

    def _mark(self):
        self.apply_btn.set_visible(sorted(self.points) != sorted(self.applied))

    # ---- geometry ----
    def _plot(self, w, h):
        return 46, 12, w - 14, h - 26  # L, T, R, B  (x0,y0,x1,y1)

    def _to_px(self, t, p, geo):
        x0, y0, x1, y1 = geo
        x = x0 + (t - TMIN) / (TMAX - TMIN) * (x1 - x0)
        y = y1 - (p / 255) * (y1 - y0)
        return x, y

    def _to_data(self, x, y, geo):
        x0, y0, x1, y1 = geo
        t = TMIN + (x - x0) / max(x1 - x0, 1) * (TMAX - TMIN)
        p = (y1 - y) / max(y1 - y0, 1) * 255
        return max(TMIN, min(TMAX, t)), max(0, min(255, p))

    # ---- drawing ----
    def _draw(self, area, cr, w, h, *_):
        geo = self._plot(w, h)
        x0, y0, x1, y1 = geo
        fg = area.get_color()
        r, g, b = fg.red, fg.green, fg.blue

        cr.set_source_rgba(r, g, b, 0.10)
        cr.set_line_width(1)
        cr.select_font_face("sans", 0, 0)
        cr.set_font_size(10)
        for pct in range(0, 101, 25):  # horizontal grid + Y labels
            yy = y1 - pct / 100 * (y1 - y0)
            cr.move_to(x0, yy); cr.line_to(x1, yy); cr.stroke()
            cr.set_source_rgba(r, g, b, 0.5); cr.move_to(6, yy + 3); cr.show_text(f"{pct}%")
            cr.set_source_rgba(r, g, b, 0.10)
        for tc in range(TMIN, TMAX + 1, 10):  # vertical grid + X labels
            xx, _ = self._to_px(tc, 0, geo)
            cr.move_to(xx, y0); cr.line_to(xx, y1); cr.stroke()
            cr.set_source_rgba(r, g, b, 0.5); cr.move_to(xx - 8, h - 8); cr.show_text(f"{tc}")
            cr.set_source_rgba(r, g, b, 0.10)

        pts = sorted(self.points)
        # filled area under the curve
        cr.set_source_rgba(0.21, 0.52, 0.89, 0.16)
        first = self._to_px(pts[0][0], pts[0][1], geo)
        cr.move_to(first[0], y1)
        for t, p in pts:
            px, py = self._to_px(t, p, geo); cr.line_to(px, py)
        last = self._to_px(pts[-1][0], pts[-1][1], geo)
        cr.line_to(last[0], y1); cr.close_path(); cr.fill()
        # curve line
        cr.set_source_rgba(0.21, 0.52, 0.89, 1.0); cr.set_line_width(2.5)
        for i, (t, p) in enumerate(pts):
            px, py = self._to_px(t, p, geo)
            cr.line_to(px, py) if i else cr.move_to(px, py)
        cr.stroke()
        # current pkg temp marker (cached — no sysfs read in the draw path)
        cur = self.cur_pkg
        if cur is not None and TMIN <= cur <= TMAX:
            mx, _ = self._to_px(cur, 0, geo)
            cr.set_source_rgba(r, g, b, 0.5); cr.set_line_width(1.2)
            cr.set_dash([4, 3], 0); cr.move_to(mx, y0); cr.line_to(mx, y1); cr.stroke()
            cr.set_dash([], 0)
            cr.move_to(mx + 3, y0 + 10); cr.show_text(f"{cur}°")
        # point handles
        for t, p in pts:
            px, py = self._to_px(t, p, geo)
            cr.set_source_rgba(0.21, 0.52, 0.89, 1.0); cr.arc(px, py, 6, 0, 6.29); cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.9); cr.arc(px, py, 2.4, 0, 6.29); cr.fill()

    # ---- interaction ----
    def _hit(self, x, y):
        geo = self._plot(self.area.get_width(), self.area.get_height())
        for i, (t, p) in enumerate(self.points):
            px, py = self._to_px(t, p, geo)
            if (px - x) ** 2 + (py - y) ** 2 <= 15 ** 2:
                return i
        return None

    def _on_begin(self, g, x, y):
        self._moved = False
        self._drag = self._hit(x, y)

    def _on_update(self, g, dx, dy):
        if self._drag is None:
            return
        self._moved = True
        ok, sx, sy = g.get_start_point()
        geo = self._plot(self.area.get_width(), self.area.get_height())
        t, p = self._to_data(sx + dx, sy + dy, geo)
        pts = self.points
        lo = pts[self._drag - 1][0] + 1 if self._drag > 0 else TMIN
        hi = pts[self._drag + 1][0] - 1 if self._drag < len(pts) - 1 else TMAX
        pts[self._drag][0] = int(max(lo, min(hi, t)))
        pts[self._drag][1] = int(round(p))
        self._update_hint()
        self.area.queue_draw()
        self._mark()

    def _on_end(self, g, dx, dy):
        self._drag = None

    def _on_right(self, g, n, x, y):
        i = self._hit(x, y)
        if i is not None and len(self.points) > 2:
            self.points.pop(i)
            self.area.queue_draw()
            self._mark()

    def _on_motion(self, c, x, y):
        geo = self._plot(self.area.get_width(), self.area.get_height())
        t, p = self._to_data(x, y, geo)
        self._cursor = (int(t), int(p / 255 * 100))
        self._update_hint()

    def _update_hint(self):
        c = self._cursor
        self.hint.set_text(f"cursor {c[0]}°C · {c[1]}%" if c else f"{len(self.points)} points")

    def _add(self, *_):
        if len(self.points) >= MAX_POINTS:
            return
        pts = sorted(self.points)
        # insert at the widest temperature gap
        gaps = [(pts[i + 1][0] - pts[i][0], i) for i in range(len(pts) - 1)]
        _, i = max(gaps) if gaps else (0, 0)
        nt = (pts[i][0] + pts[i + 1][0]) // 2
        npv = (pts[i][1] + pts[i + 1][1]) // 2
        self.points = pts[:i + 1] + [[nt, npv]] + pts[i + 1:]
        self.area.queue_draw()
        self._mark()

    def _on_preset(self, dd, _):
        i = dd.get_selected()
        if i > 0:
            name = list(PRESETS)[i - 1]
            self.points = [list(p) for p in PRESETS[name]]
            self.area.queue_draw()
            self._mark()
            self.window.toast(f"Loaded “{name}” preset — press Apply to write it")
            dd.set_selected(0)

    def _reload(self):
        self.points = self.gpu.read_curve()
        self.applied = [list(p) for p in self.points]
        self.area.queue_draw()
        self._mark()
        self.window.toast("Reloaded the curve currently on the GPU")

    def _stock(self, *_):
        # hand the fan back to the card's stock auto table (pwm1_enable=2)
        run_priv(["xe-fan-curve", "auto"], self.window)
        self.window.toast("Reverting the fan to the stock auto table…")

    def _apply(self, *_):
        pts = sorted(self.points)
        args = ["xe-fan-curve", "set"] + [f"{int(t)}:{int(p)}" for t, p in pts]
        run_priv(args, self.window)
        self.applied = [list(p) for p in pts]
        self._mark()
        self.window.toast(f"Applying fan curve ({len(pts)} points)…")


# ---------------------------------------------------------------- window
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Arc GPU Dashboard")
        self.set_default_size(900, 600)
        self.gpu = XeGpu()

        prov = Gtk.CssProvider(); prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        hb.set_title_widget(switcher)
        hb.pack_start(icon_button("view-refresh-symbolic", "Refresh readings now",
                                  lambda *_: self.refresh()))
        tv.add_top_bar(hb)
        tv.set_content(self.stack)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        if not self.gpu.present:
            self.stack.add_titled(Gtk.Label(label="No Intel xe GPU found", margin_top=40),
                                  "none", "No GPU")
            return

        dash = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                       margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); left.set_size_request(330, -1)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        dash.append(left); dash.append(right)
        self._build_stats(left); self._build_controls(left); self._build_temps(right)
        p1 = self.stack.add_titled(dash, "dash", "Dashboard")
        p1.set_icon_name("utilities-system-monitor-symbolic")

        self.editor = CurveEditor(self.gpu, self)
        curve_wrap = Gtk.Box(margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        curve_wrap.append(self.editor)
        p2 = self.stack.add_titled(curve_wrap, "curve", "Fan Curve")
        p2.set_icon_name("power-profile-balanced-symbolic")

        self.tcache = {}          # label -> °C
        self._reading = False
        self._tick()              # first async snapshot
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def toast(self, msg, ms=2500):
        # timeout=0 keeps Adw from auto-dismissing; we dismiss manually for a precise 2.5s.
        t = Adw.Toast(title=msg, timeout=0)
        self.toasts.add_toast(t)
        GLib.timeout_add(ms, lambda: (t.dismiss(), False)[1])

    def _build_stats(self, parent):
        c = card("GPU", "Live readings — Intel Arc (xe) driver via sysfs.")
        g = Gtk.Grid(column_spacing=10, row_spacing=4)
        self.v_id = kv(g, 0, "device", Gtk.Label(xalign=0))
        self.v_clk = kv(g, 1, "clock", Gtk.Label(xalign=0),
                        "Current GPU clock, with the configured min–max and hardware range.")
        self.freq_bar = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True)
        g.attach(self.freq_bar, 0, 2, 2, 1)
        self.v_pwr = kv(g, 3, "power", Gtk.Label(xalign=0),
                        "Power cap (TDP) and the firmware I1 crit limit.")
        self.v_fan = kv(g, 4, "fan", Gtk.Label(xalign=0),
                        "Fan RPM, current duty, and mode (manual curve / auto / full).")
        c.append(g); parent.append(c)

    def _build_controls(self, parent):
        c = card("Controls", "Writes run the xe-* helpers via pkexec (asks for your password).")
        fan = Gtk.Box(spacing=6)
        fl = Gtk.Label(label="Fan", xalign=0, width_chars=5); fl.add_css_class("dim")
        fan.append(fl)
        fan.append(icon_button("document-edit-symbolic", "Open the fan-curve editor",
                               lambda *_: self.stack.set_visible_child_name("curve"),
                               label="Curve"))
        fan.append(icon_button("power-profile-balanced-symbolic",
                               "Auto: hand the fan back to the card's stock table",
                               lambda *_: run_priv(["xe-fan-curve", "auto"], self, self.refresh)))
        fan.append(icon_button("power-profile-performance-symbolic",
                               "Max: run the fan at full speed",
                               lambda *_: run_priv(["xe-fan-curve", "max"], self, self.refresh)))
        c.append(fan)

        cl = self.gpu.clocks()
        lo, hi = cl.get("rpn") or 400, cl.get("rp0") or 2400
        self.sp_pow = self._spin(50, 400, 10, self.gpu.power().get("cap_w") or 190)
        self.sp_min = self._spin(lo, hi, 50, cl.get("min") or lo)
        self.sp_max = self._spin(lo, hi, 50, cl.get("max") or hi)
        g = Gtk.Grid(column_spacing=10, row_spacing=6)
        kv(g, 0, "Power cap (W)", self.sp_pow, "Board power limit (TDP). Lower = cooler/quieter.")
        kv(g, 1, "Min clock (MHz)", self.sp_min,
           "Idle clock floor. Lowering it (e.g. 400) drops idle power/heat; still boosts under load.")
        kv(g, 2, "Max clock (MHz)", self.sp_max, "Clock ceiling. Lower for less heat/noise under load.")
        c.append(g)
        self.tune_base = self._tune_vals()
        for sp in (self.sp_pow, self.sp_min, self.sp_max):
            sp.connect("value-changed", lambda *_: self._mark_tune())
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        btns.append(icon_button("edit-undo-symbolic", "Reset power & clocks to hardware defaults",
                                self._reset_tune, label="Reset"))
        self.tune_apply = icon_button("emblem-ok-symbolic", "Apply power cap and clock limits",
                                      self.on_apply, label="Apply", css="suggested-action")
        btns.append(self.tune_apply)
        c.append(btns); parent.append(c)
        self._mark_tune()

    def _tune_vals(self):
        return (self.sp_pow.get_value(), self.sp_min.get_value(), self.sp_max.get_value())

    def _mark_tune(self):
        self.tune_apply.set_visible(self._tune_vals() != self.tune_base)

    def _reset_tune(self, *_):
        run_priv(["xe-gpu-tune", "reset"], self, self._after_reset)
        self.toast("Resetting power & clocks to hardware defaults…")

    def _after_reset(self):
        self.refresh()
        cl = self.gpu.clocks(); pw = self.gpu.power()
        if cl.get("min"):
            self.sp_min.set_value(cl["min"])
        if cl.get("max"):
            self.sp_max.set_value(cl["max"])
        if pw.get("cap_w"):
            self.sp_pow.set_value(pw["cap_w"])
        self.tune_base = self._tune_vals()
        self._mark_tune()

    def _build_temps(self, parent):
        c = card("Temperatures", "All sensors the driver exposes. Colour = headroom to the crit limit.")
        c.set_vexpand(True)
        self.main_t = {}
        mg = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        for i, name in enumerate(("pkg", "mctrl", "pcie", "vram")):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2); cell.add_css_class("chip")
            lab = Gtk.Label(label=name, xalign=0); lab.add_css_class("clbl")
            val = Gtk.Label(label="-", xalign=0); val.add_css_class("big")
            bar = Gtk.LevelBar(min_value=0, max_value=110, hexpand=True)
            cell.append(lab); cell.append(val); cell.append(bar)
            mg.attach(cell, i % 2, i // 2, 1, 1)
            self.main_t[name] = (cell, val, bar)
        c.append(mg)
        vh = Gtk.Label(label="VRAM CHANNELS", xalign=0); vh.add_css_class("section"); vh.set_margin_top(4)
        c.append(vh)
        self.vram_chips = {}
        self.vram_grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        c.append(self.vram_grid); parent.append(c)

    def _spin(self, lo, hi, step, val):
        s = Gtk.SpinButton(adjustment=Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                                                     value=val), valign=Gtk.Align.CENTER)
        s.set_numeric(True)
        return s

    def on_apply(self, _b):
        run_priv(["xe-gpu-tune", "set", "--power-w", str(int(self.sp_pow.get_value())),
                  "--clk-min", str(int(self.sp_min.get_value())),
                  "--clk-max", str(int(self.sp_max.get_value()))], self, self.refresh)
        self.tune_base = self._tune_vals()
        self._mark_tune()
        self.toast(f"Applying: {int(self.sp_pow.get_value())} W · "
                   f"{int(self.sp_min.get_value())}–{int(self.sp_max.get_value())} MHz")

    def _tc(self, w, cls):
        for x in ("t-cool", "t-warm", "t-hot"):
            w.remove_css_class(x)
        w.add_css_class(cls)

    def refresh(self, *_):
        self._tick()      # trigger an async read (used by control after-callbacks)
        return False

    def _tick(self):
        # ALL sysfs reads run off the main thread — the first read after GPU idle
        # forces a wake (~0.8-1.4s); doing it here would freeze the UI.
        if self._reading:
            return True
        self._reading = True

        def work():
            try:
                data = self.gpu.snapshot()
            except Exception:
                data = None
            GLib.idle_add(self._apply, data)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _apply(self, data):
        self._reading = False
        if not data:
            return False
        ident = data["id"]
        self.v_id.set_text(f"{ident['card']} · {ident['pci']} · {ident['id']}")
        cl = data["clocks"]
        self.v_clk.set_markup(f"<b>{cl.get('cur','?')}</b> MHz  <span alpha='55%'>"
                              f"({cl.get('min','?')}–{cl.get('max','?')}, hw {cl.get('rpn','?')}–{cl.get('rp0','?')})</span>")
        if cl.get("cur") and cl.get("rp0"):
            lo = cl.get("rpn") or 0
            self.freq_bar.set_max_value(max(cl["rp0"] - lo, 1))
            self.freq_bar.set_value(max(min(cl["cur"] - lo, cl["rp0"] - lo), 0))
        pw = data["power"]
        cap = f"{pw['cap_w']} W" if pw.get("cap_w") else "unset"
        crit = f"  ·  I1 {pw['crit_w']:.1f} W" if pw.get("crit_w") else ""
        self.v_pwr.set_markup(f"cap <b>{cap}</b>{crit}")
        fn = data["fan"]
        duty = f" ({round((fn['duty'] or 0)/255*100)}%)" if fn.get("duty") is not None else ""
        fmax = f" / {fn['max']}" if fn.get("max") else ""
        self.v_fan.set_markup(f"<b>{fn.get('rpm','?')}</b> rpm{fmax}{duty}  <span alpha='55%'>· {fn.get('mode','?')}</span>")

        mains = {t["label"]: t for t in data["mains"]}
        for t in data["mains"] + data["vram"]:
            self.tcache[t["label"]] = t["c"]
        if "pkg" in mains:
            self.editor.cur_pkg = mains["pkg"]["c"]
        hottest = max(self.tcache.values(), default=None)
        for name, (cell, val, bar) in self.main_t.items():
            t = mains.get(name)
            if not t:
                val.set_text("—"); continue
            val.set_text(f"{t['c']}°{' 🔥' if t['c'] == hottest else ''}")
            bar.set_max_value(t["crit"] or 110); bar.set_value(min(t["c"], t["crit"] or 110))
            self._tc(cell, tclass(t["c"], t["crit"])); self._tc(val, tclass(t["c"], t["crit"]))
        for i, t in enumerate(sorted(data["vram"], key=lambda x: int(x["label"].rsplit("_", 1)[-1]))):
            key = t["label"]
            if key not in self.vram_chips:
                chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); chip.add_css_class("chip")
                cl_ = Gtk.Label(label="ch" + key.rsplit("_", 1)[-1], xalign=0.5); cl_.add_css_class("clbl")
                cv = Gtk.Label(xalign=0.5); cv.add_css_class("cval")
                chip.append(cl_); chip.append(cv)
                self.vram_grid.attach(chip, i % 4, i // 4, 1, 1)
                self.vram_chips[key] = (chip, cv)
            chip, cv = self.vram_chips[key]
            cv.set_text(f"{t['c']}°{' 🔥' if t['c'] == hottest else ''}")
            self._tc(chip, tclass(t["c"], t["crit"]))
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.exzile.XeGpuDashboard",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    App().run(None)
