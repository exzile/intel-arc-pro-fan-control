#!/usr/bin/env python3
# xe-gpu-gui — native GTK4/libadwaita dashboard for the Intel Arc (xe) GPU.
# Live monitor (fan / clocks / power / all temperatures) + controls that call the
# xe-fan-curve / xe-gpu-tune helpers via pkexec (privileged writes are prompted by polkit).
# Reads are unprivileged sysfs; no kernel poking here.
import os, glob, subprocess
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio  # noqa: E402

REFRESH_SECONDS = 2


# ---------------------------------------------------------------- data layer
def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path):
    v = _read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class XeGpu:
    """Locate the xe GPU's sysfs card + hwmon and read everything on demand."""

    def __init__(self):
        self.card = None
        self.hwmon = None
        self._find()

    def _find(self):
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
        return self.hwmon is not None or self.card is not None

    def identity(self):
        if not self.card:
            return {"card": "—", "pci": "—", "id": "—"}
        pci = os.path.basename(os.path.realpath(os.path.join(self.card, "device")))
        did = _read(os.path.join(self.card, "device", "device")) or ""
        return {"card": os.path.basename(self.card), "pci": pci,
                "id": "8086:" + did.replace("0x", "")}

    def clocks(self):
        gt = os.path.join(self.card or "", "device/tile0/gt0/freq0")
        g = lambda n: _read_int(os.path.join(gt, n))  # noqa: E731
        return {"cur": g("cur_freq"), "min": g("min_freq"), "max": g("max_freq"),
                "rpn": g("rpn_freq"), "rp0": g("rp0_freq")}

    def power(self):
        if not self.hwmon:
            return {}
        cap = _read_int(os.path.join(self.hwmon, "power1_cap"))
        crit = _read_int(os.path.join(self.hwmon, "power1_crit"))
        return {"cap_w": (cap // 1_000_000) if cap else None,
                "crit_w": (crit / 1_000_000) if crit else None}

    def fan(self):
        if not self.hwmon:
            return {}
        pwm = _read_int(os.path.join(self.hwmon, "pwm1_enable"))
        mode = {0: "full speed", 1: "manual curve", 2: "auto (stock)"}.get(pwm, "?")
        return {"rpm": _read_int(os.path.join(self.hwmon, "fan1_input")),
                "max": _read_int(os.path.join(self.hwmon, "fan1_max")),
                "mode": mode, "pwm": pwm}

    def temps(self):
        out = []
        if not self.hwmon:
            return out
        for f in sorted(glob.glob(os.path.join(self.hwmon, "temp*_input"))):
            n = os.path.basename(f)[4:-6]  # tempN_input -> N
            inp = _read_int(f)
            if inp is None:
                continue
            base = os.path.join(self.hwmon, f"temp{n}")
            lbl = _read(base + "_label") or f"temp{n}"
            crit = _read_int(base + "_crit")
            out.append({"label": lbl, "c": inp // 1000,
                        "crit": (crit // 1000) if crit else None})
        return out


# ---------------------------------------------------------------- helpers
def run_priv(args, parent):
    """Run a toolkit helper with pkexec; report failures in a toast/dialog."""
    try:
        subprocess.Popen(["pkexec"] + args)
    except OSError as e:
        d = Adw.MessageDialog(transient_for=parent, heading="Command failed",
                              body=f"{' '.join(args)}\n{e}")
        d.add_response("ok", "OK")
        d.present()


def temp_css_class(c, crit):
    if crit and c >= crit - 10:
        return "error"
    if crit and c >= crit - 25:
        return "warning"
    return "success"


# ---------------------------------------------------------------- UI
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Arc GPU Dashboard")
        self.set_default_size(560, 820)
        self.gpu = XeGpu()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_lbl = Adw.WindowTitle(title="Arc GPU Dashboard", subtitle="Intel xe")
        header.set_title_widget(self.title_lbl)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh now")
        refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        self.set_content(toolbar)

        if not self.gpu.present:
            g = Adw.PreferencesGroup(title="No Intel xe GPU found")
            g.set_description("The xe driver isn't loaded, or no Arc GPU is present.")
            page.add(g)
            return

        # --- Status group ---
        self.status_group = Adw.PreferencesGroup(title="GPU")
        self.row_card = Adw.ActionRow(title="Device")
        self.row_clocks = Adw.ActionRow(title="Clocks")
        self.row_power = Adw.ActionRow(title="Power")
        for r in (self.row_card, self.row_clocks, self.row_power):
            r.add_css_class("property")
            self.status_group.add(r)
        page.add(self.status_group)

        # --- Fan group (with controls) ---
        self.fan_group = Adw.PreferencesGroup(title="Fan")
        self.row_fan = Adw.ActionRow(title="Speed")
        self.row_fan.add_css_class("property")
        btnbox = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for label, args in (("Curve", ["xe-fan-curve", "boot"]),
                            ("Auto", ["xe-fan-curve", "auto"]),
                            ("Max", ["xe-fan-curve", "max"])):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _w, a=args: (run_priv(a, self), GLib.timeout_add(800, self.refresh)))
            btnbox.append(b)
        self.row_fan.add_suffix(btnbox)
        self.fan_group.add(self.row_fan)
        page.add(self.fan_group)

        # --- Tuning group (power cap + clock max) ---
        self.tune_group = Adw.PreferencesGroup(
            title="Tuning", description="Applied via xe-gpu-tune (needs authorization)")
        cl = self.gpu.clocks()
        lo, hi = cl.get("rpn") or 400, cl.get("rp0") or 2400
        self.power_spin = self._spin(50, 400, 10, self.gpu.power().get("cap_w") or 190)
        self.clkmin_spin = self._spin(lo, hi, 50, cl.get("min") or lo)
        self.clkmax_spin = self._spin(lo, hi, 50, cl.get("max") or hi)
        prow = Adw.ActionRow(title="Power cap", subtitle="watts")
        prow.add_suffix(self.power_spin)
        mrow = Adw.ActionRow(title="Min clock", subtitle="MHz — idle floor (low = cooler idle)")
        mrow.add_suffix(self.clkmin_spin)
        crow = Adw.ActionRow(title="Max clock", subtitle="MHz")
        crow.add_suffix(self.clkmax_spin)
        apply_btn = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self.on_apply_tune)
        reset_btn = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        reset_btn.connect("clicked", lambda *_: (run_priv(["xe-gpu-tune", "reset"], self),
                                                 GLib.timeout_add(800, self.refresh)))
        arow = Adw.ActionRow(title="")
        abox = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        abox.append(reset_btn)
        abox.append(apply_btn)
        arow.add_suffix(abox)
        for r in (prow, mrow, crow, arow):
            self.tune_group.add(r)
        page.add(self.tune_group)

        # --- Temperatures group ---
        self.temp_group = Adw.PreferencesGroup(title="Temperatures")
        page.add(self.temp_group)
        self._temp_rows = {}

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def _spin(self, lo, hi, step, val):
        adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, value=val)
        s = Gtk.SpinButton(adjustment=adj, valign=Gtk.Align.CENTER)
        s.set_numeric(True)
        return s

    def on_apply_tune(self, _btn):
        args = ["xe-gpu-tune", "set",
                "--power-w", str(int(self.power_spin.get_value())),
                "--clk-min", str(int(self.clkmin_spin.get_value())),
                "--clk-max", str(int(self.clkmax_spin.get_value()))]
        run_priv(args, self)
        GLib.timeout_add(900, self.refresh)

    def _tick(self):
        self.refresh()
        return True  # keep the timer

    def refresh(self, *_):
        g = self.gpu
        ident = g.identity()
        self.row_card.set_subtitle(f"{ident['card']} · {ident['pci']} · {ident['id']}")
        cl = g.clocks()
        self.row_clocks.set_subtitle(
            f"{cl.get('cur','?')} / {cl.get('min','?')} / {cl.get('max','?')} MHz "
            f"(hw {cl.get('rpn','?')}–{cl.get('rp0','?')})")
        pw = g.power()
        cap = f"{pw['cap_w']} W" if pw.get("cap_w") else "unset"
        crit = f" · I1 crit {pw['crit_w']:.2f} W" if pw.get("crit_w") else ""
        self.row_power.set_subtitle(f"cap {cap}{crit}")
        fan = g.fan()
        fmax = f" / {fan['max']} max" if fan.get("max") else ""
        self.row_fan.set_subtitle(f"{fan.get('rpm','?')} rpm{fmax}  ·  {fan.get('mode','?')}")

        # temps: create rows once, update bars each tick
        temps = g.temps()
        hottest = max((t["c"] for t in temps), default=None)
        for t in temps:
            key = t["label"]
            if key not in self._temp_rows:
                row = Adw.ActionRow(title=key)
                bar = Gtk.LevelBar(min_value=0, max_value=(t["crit"] or 110),
                                   hexpand=True, valign=Gtk.Align.CENTER)
                bar.set_size_request(180, -1)
                val = Gtk.Label(width_chars=6, xalign=1.0)
                box = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
                box.append(bar)
                box.append(val)
                row.add_suffix(box)
                self.temp_group.add(row)
                self._temp_rows[key] = (row, bar, val)
            row, bar, val = self._temp_rows[key]
            bar.set_value(min(t["c"], bar.get_max_value()))
            hot = " 🔥" if t["c"] == hottest else ""
            val.set_text(f"{t['c']}°C{hot}")
            for c in ("error", "warning", "success"):
                val.remove_css_class(c)
            val.add_css_class(temp_css_class(t["c"], t["crit"]))
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.exzile.XeGpuDashboard",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    App().run(None)
