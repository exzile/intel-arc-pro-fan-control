#!/usr/bin/env python3
# xe-gpu-gui — native GTK4/libadwaita dashboard for the Intel Arc (xe) GPU.
# Compact two-pane layout: live stats + controls (left), dense temperature grid (right).
# Controls call xe-fan-curve / xe-gpu-tune via pkexec (polkit prompts for privileged writes).
# Reads are unprivileged sysfs; no kernel poking here.
import os, glob, subprocess
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk  # noqa: E402

REFRESH_SECONDS = 2

CSS = b"""
.card2 { background: @card_bg_color; border-radius: 12px; padding: 12px 14px; }
.section { font-weight: bold; opacity: 0.7; font-size: 0.80em; letter-spacing: .04em; }
.big { font-size: 1.7em; font-weight: 800; }
.unit { opacity: 0.55; font-size: 0.82em; }
.dim { opacity: 0.60; font-size: 0.86em; }
.chip { border-radius: 9px; padding: 7px 6px; background: alpha(@window_fg_color,0.05); }
.chip .cval { font-weight: 700; }
.chip .clbl { opacity: 0.60; font-size: 0.74em; }
.t-cool { color: #3584e4; } .chip.t-cool { background: alpha(#3584e4,0.12); }
.t-warm { color: #e5a50a; } .chip.t-warm { background: alpha(#e5a50a,0.16); }
.t-hot  { color: #e01b24; } .chip.t-hot  { background: alpha(#e01b24,0.18); }
.hotmark { font-weight: 800; }
"""


# ---------------------------------------------------------------- data layer
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
                "mode": {0: "full", 1: "manual curve", 2: "auto"}.get(pwm, "?")}

    def temps(self):
        out = []
        for f in sorted(glob.glob(os.path.join(self.hwmon or "_", "temp*_input"))):
            n = os.path.basename(f)[4:-6]
            inp = _int(f)
            if inp is None:
                continue
            base = os.path.join(self.hwmon, f"temp{n}")
            crit = _int(base + "_crit")
            out.append({"label": _read(base + "_label") or f"temp{n}",
                        "c": inp // 1000, "crit": (crit // 1000) if crit else None})
        return out


def tclass(c, crit):
    if crit and c >= crit - 10:
        return "t-hot"
    if crit and c >= crit - 25:
        return "t-warm"
    return "t-cool"


def run_priv(args, parent, after=None):
    try:
        p = subprocess.Popen(["pkexec"] + args)
        if after:
            GLib.timeout_add(1000, lambda: (after(), False)[1])
        return p
    except OSError as e:
        d = Adw.MessageDialog(transient_for=parent, heading="Command failed",
                              body=f"{' '.join(args)}\n{e}")
        d.add_response("ok", "OK")
        d.present()


# ---------------------------------------------------------------- widgets
def card(title):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.add_css_class("card2")
    lbl = Gtk.Label(label=title.upper(), xalign=0)
    lbl.add_css_class("section")
    box.append(lbl)
    return box


def kv_grid():
    g = Gtk.Grid(column_spacing=10, row_spacing=4)
    return g


def kv(grid, row, key, val_widget):
    k = Gtk.Label(label=key, xalign=0)
    k.add_css_class("dim")
    grid.attach(k, 0, row, 1, 1)
    grid.attach(val_widget, 1, row, 1, 1)
    return val_widget


# ---------------------------------------------------------------- window
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Arc GPU Dashboard")
        self.set_default_size(880, 560)
        self.gpu = XeGpu()

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Arc GPU Dashboard", subtitle="Intel xe"))
        rb = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        rb.connect("clicked", lambda *_: self.refresh())
        hb.pack_start(rb)
        tv.add_top_bar(hb)
        self.set_content(tv)

        if not self.gpu.present:
            b = Gtk.Label(label="No Intel xe GPU found (driver not loaded?)", margin_top=40)
            tv.set_content(b)
            return

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                       margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        tv.set_content(root)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_size_request(340, -1)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        root.append(left)
        root.append(right)

        self._build_stats(left)
        self._build_controls(left)
        self._build_temps(right)

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, lambda: (self.refresh(), True)[1])

    # ---- left: live stats ----
    def _build_stats(self, parent):
        c = card("GPU")
        g = kv_grid()
        self.v_id = kv(g, 0, "device", Gtk.Label(xalign=0))
        self.v_clk = kv(g, 1, "clock", Gtk.Label(xalign=0))
        self.freq_bar = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True)
        g.attach(self.freq_bar, 0, 2, 2, 1)
        self.v_pwr = kv(g, 3, "power", Gtk.Label(xalign=0))
        self.v_fan = kv(g, 4, "fan", Gtk.Label(xalign=0))
        for w in (self.v_id, self.v_clk, self.v_pwr, self.v_fan):
            w.set_wrap(False)
        c.append(g)
        parent.append(c)

    # ---- left: controls ----
    def _build_controls(self, parent):
        c = card("Controls")
        # fan buttons
        fanrow = Gtk.Box(spacing=6)
        fanrow.append(Gtk.Label(label="Fan", xalign=0, width_chars=6))
        for label, args in (("Curve", ["xe-fan-curve", "boot"]),
                            ("Auto", ["xe-fan-curve", "auto"]),
                            ("Max", ["xe-fan-curve", "max"])):
            b = Gtk.Button(label=label, hexpand=True)
            b.connect("clicked", lambda _w, a=args: run_priv(a, self, self.refresh))
            fanrow.append(b)
        c.append(fanrow)

        cl = self.gpu.clocks()
        lo, hi = cl.get("rpn") or 400, cl.get("rp0") or 2400
        self.sp_pow = self._spin(50, 400, 10, self.gpu.power().get("cap_w") or 190)
        self.sp_min = self._spin(lo, hi, 50, cl.get("min") or lo)
        self.sp_max = self._spin(lo, hi, 50, cl.get("max") or hi)
        grid = kv_grid()
        kv(grid, 0, "Power cap (W)", self.sp_pow)
        kv(grid, 1, "Min clock (MHz)", self.sp_min)
        kv(grid, 2, "Max clock (MHz)", self.sp_max)
        hint = Gtk.Label(label="min = idle floor · low = cooler idle", xalign=0)
        hint.add_css_class("dim")
        grid.attach(hint, 0, 3, 2, 1)
        c.append(grid)

        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        rst = Gtk.Button(label="Reset")
        rst.connect("clicked", lambda *_: run_priv(["xe-gpu-tune", "reset"], self, self.refresh))
        app = Gtk.Button(label="Apply")
        app.add_css_class("suggested-action")
        app.connect("clicked", self.on_apply)
        btns.append(rst)
        btns.append(app)
        c.append(btns)
        parent.append(c)

    # ---- right: temperatures ----
    def _build_temps(self, parent):
        c = card("Temperatures")
        c.set_vexpand(True)
        # main sensors (pkg/mctrl/pcie/vram) as a 2x2 of mini cards
        self.main_t = {}
        mg = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        for i, name in enumerate(("pkg", "mctrl", "pcie", "vram")):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            cell.add_css_class("chip")
            lab = Gtk.Label(label=name, xalign=0)
            lab.add_css_class("clbl")
            val = Gtk.Label(label="-", xalign=0)
            val.add_css_class("big")
            bar = Gtk.LevelBar(min_value=0, max_value=110, hexpand=True)
            cell.append(lab)
            cell.append(val)
            cell.append(bar)
            mg.attach(cell, i % 2, i // 2, 1, 1)
            self.main_t[name] = (cell, val, bar)
        c.append(mg)

        vh = Gtk.Label(label="VRAM CHANNELS", xalign=0)
        vh.add_css_class("section")
        vh.set_margin_top(4)
        c.append(vh)
        self.vram_chips = {}
        self.vram_grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        c.append(self.vram_grid)
        parent.append(c)

    def _spin(self, lo, hi, step, val):
        s = Gtk.SpinButton(adjustment=Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                                                     value=val), valign=Gtk.Align.CENTER)
        s.set_numeric(True)
        return s

    def on_apply(self, _b):
        run_priv(["xe-gpu-tune", "set",
                  "--power-w", str(int(self.sp_pow.get_value())),
                  "--clk-min", str(int(self.sp_min.get_value())),
                  "--clk-max", str(int(self.sp_max.get_value()))], self, self.refresh)

    def _set_temp_class(self, widget, cls):
        for x in ("t-cool", "t-warm", "t-hot"):
            widget.remove_css_class(x)
        widget.add_css_class(cls)

    def refresh(self, *_):
        g = self.gpu
        ident = g.identity()
        self.v_id.set_text(f"{ident['card']} · {ident['pci']} · {ident['id']}")
        cl = g.clocks()
        self.v_clk.set_markup(
            f"<b>{cl.get('cur','?')}</b> MHz  <span alpha='60%'>({cl.get('min','?')}–{cl.get('max','?')}, hw {cl.get('rpn','?')}–{cl.get('rp0','?')})</span>")
        if cl.get("cur") and cl.get("rp0"):
            lo = cl.get("rpn") or 0
            self.freq_bar.set_max_value(max(cl["rp0"] - lo, 1))
            self.freq_bar.set_value(max(min(cl["cur"] - lo, cl["rp0"] - lo), 0))
        pw = g.power()
        cap = f"{pw['cap_w']} W" if pw.get("cap_w") else "unset"
        crit = f"  ·  I1 {pw['crit_w']:.1f} W" if pw.get("crit_w") else ""
        self.v_pwr.set_markup(f"cap <b>{cap}</b>{crit}")
        fn = g.fan()
        fmax = f" / {fn['max']}" if fn.get("max") else ""
        self.v_fan.set_markup(f"<b>{fn.get('rpm','?')}</b> rpm{fmax}  <span alpha='60%'>· {fn.get('mode','?')}</span>")

        temps = {t["label"]: t for t in g.temps()}
        hottest = max((t["c"] for t in temps.values()), default=None)
        # main sensors
        for name, (cell, val, bar) in self.main_t.items():
            t = temps.get(name)
            if not t:
                val.set_text("—")
                continue
            hot = " 🔥" if t["c"] == hottest else ""
            val.set_text(f"{t['c']}°{hot}")
            bar.set_max_value(t["crit"] or 110)
            bar.set_value(min(t["c"], t["crit"] or 110))
            self._set_temp_class(cell, tclass(t["c"], t["crit"]))
            self._set_temp_class(val, tclass(t["c"], t["crit"]))
        # vram channels
        chans = sorted((t for lbl, t in temps.items() if lbl.startswith("vram_ch_")),
                       key=lambda t: int(t["label"].rsplit("_", 1)[-1]))
        for i, t in enumerate(chans):
            key = t["label"]
            if key not in self.vram_chips:
                chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                chip.add_css_class("chip")
                cl_ = Gtk.Label(label="ch" + key.rsplit("_", 1)[-1], xalign=0.5)
                cl_.add_css_class("clbl")
                cv = Gtk.Label(xalign=0.5)
                cv.add_css_class("cval")
                chip.append(cl_)
                chip.append(cv)
                self.vram_grid.attach(chip, i % 4, i // 4, 1, 1)
                self.vram_chips[key] = (chip, cv)
            chip, cv = self.vram_chips[key]
            mark = " 🔥" if t["c"] == hottest else ""
            cv.set_text(f"{t['c']}°{mark}")
            self._set_temp_class(chip, tclass(t["c"], t["crit"]))
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.exzile.XeGpuDashboard",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    App().run(None)
