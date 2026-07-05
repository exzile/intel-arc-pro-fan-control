#!/usr/bin/env python3
# xe-gpu-gui — native GTK4/libadwaita control panel for the Intel Arc (xe) GPU.
# Tabs: Dashboard (live stats + tuning), Fan Curve (graphical draggable editor),
# and Overclock (voltage-frequency curve graph + offset slider — shown when the
# xe_gt_oc patch exposes .../gt0/oc/vf_curve).
# Controls call xe-fan-curve / xe-gpu-tune / xe-gpu-oc via pkexec (polkit prompts
# for writes). Reads are unprivileged sysfs; no kernel poking here.
import os, glob, subprocess, threading, collections, json
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
.chip { border-radius: 9px; padding: 8px 8px; background: alpha(@window_fg_color,0.05);
        border: 2px solid alpha(@window_fg_color,0.16);
        transition: border-color 220ms ease, background 220ms ease; }
.chip .cval { font-weight: 700; }
.chip .clbl { opacity: 0.60; font-size: 0.74em; }
/* graduated temperature scale by headroom to crit: teal(cool) -> green -> lime ->
   yellow -> orange -> red(hot). the whole border + tint + value colour shift with heat. */
.tb0{color:#22c3a6;} .chip.tb0{background:alpha(#22c3a6,0.12);border-color:#22c3a6;}
.tb1{color:#2fd07f;} .chip.tb1{background:alpha(#2fd07f,0.13);border-color:#2fd07f;}
.tb2{color:#9ad12f;} .chip.tb2{background:alpha(#9ad12f,0.14);border-color:#9ad12f;}
.tb3{color:#f5c211;} .chip.tb3{background:alpha(#f5c211,0.15);border-color:#f5c211;}
.tb4{color:#ff9f45;} .chip.tb4{background:alpha(#ff9f45,0.16);border-color:#ff9f45;}
.tb5{color:#ff6b3d;} .chip.tb5{background:alpha(#ff6b3d,0.17);border-color:#ff6b3d;}
.tb6{color:#ff4d4d;} .chip.tb6{background:alpha(#ff4d4d,0.19);border-color:#ff4d4d;}
.info { opacity:0.45; }

/* --- dashboard metric tiles (Windows-style live panel w/ sparklines) --- */
.mtile  { background: alpha(@window_fg_color,0.05); border-radius: 12px; padding: 9px 11px;
          border: 2px solid alpha(@window_fg_color,0.10);
          transition: border-color 220ms ease, background 220ms ease; }
.mtile.tb0{border-color:alpha(#22c3a6,0.6);background:alpha(#22c3a6,0.10);}
.mtile.tb1{border-color:alpha(#2fd07f,0.6);background:alpha(#2fd07f,0.10);}
.mtile.tb2{border-color:alpha(#9ad12f,0.6);background:alpha(#9ad12f,0.10);}
.mtile.tb3{border-color:alpha(#f5c211,0.6);background:alpha(#f5c211,0.11);}
.mtile.tb4{border-color:alpha(#ff9f45,0.7);background:alpha(#ff9f45,0.12);}
.mtile.tb5{border-color:alpha(#ff6b3d,0.7);background:alpha(#ff6b3d,0.13);}
.mtile.tb6{border-color:alpha(#ff4d4d,0.8);background:alpha(#ff4d4d,0.15);}
.mlabel { font-size:0.70em; font-weight:800; opacity:0.55; letter-spacing:.06em; }
.mvalue { font-size:1.7em; font-weight:800; }
.mvalue.tb0{color:#22c3a6;} .mvalue.tb1{color:#2fd07f;} .mvalue.tb2{color:#9ad12f;}
.mvalue.tb3{color:#f5c211;} .mvalue.tb4{color:#ff9f45;} .mvalue.tb5{color:#ff6b3d;}
.mvalue.tb6{color:#ff4d4d;}
.munit  { opacity:0.50; font-size:0.9em; }
.msub   { opacity:0.55; font-size:0.76em; }
.filter-group { font-size:0.72em; font-weight:800; opacity:0.5; letter-spacing:.05em; margin-top:6px; }

/* --- overclock form: aligned rows, icons, animated controls --- */
.field-icon  { opacity:0.72; }
.field-label { min-width: 118px; }
.field-unit  { opacity:0.55; font-size:0.86em; }
.field-changed { color:@accent_color; font-weight:800; }
.field-uv { color:#2ec27e; font-weight:800; }   /* undervolt: green */
.field-ov { color:#e5a50a; font-weight:800; }   /* overvolt: amber */
.field-hot { color:#e01b24; font-weight:800; }  /* raised temp/limit: red */
.field-spin  { transition: box-shadow 160ms ease; }
.mode-row    { padding: 2px 2px 4px 2px; }

.oc-scale trough    { min-height:6px; border-radius:6px; transition: background 180ms ease; }
.oc-scale highlight { border-radius:6px; transition: background 180ms ease; }
.oc-scale:hover highlight { background:@accent_color; }
.oc-scale slider    { transition: transform 120ms ease, box-shadow 160ms ease; }
.oc-scale:hover slider { transform: scale(1.14); box-shadow: 0 0 0 4px alpha(@accent_color,0.18); }

.oc-page { animation: ocfade 280ms ease; }
@keyframes ocfade { from { opacity:0; } to { opacity:1; } }

button.suggested-action.pulse { animation: ocpulse 1.6s ease-in-out infinite; }
@keyframes ocpulse {
  0%,100% { box-shadow: 0 0 0 0 alpha(@accent_color,0.55); }
  50%     { box-shadow: 0 0 0 6px alpha(@accent_color,0.0); }
}
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
        return {"cur": g("cur_freq"), "act": g("act_freq"), "min": g("min_freq"),
                "max": g("max_freq"), "rpn": g("rpn_freq"), "rp0": g("rp0_freq")}

    def power(self):
        if not self.hwmon:
            return {}
        cap = _int(os.path.join(self.hwmon, "power1_cap"))
        crit = _int(os.path.join(self.hwmon, "power1_crit"))
        return {"cap_w": (cap // 1_000_000) if cap else None,
                "crit_w": (crit / 1_000_000) if crit else None}

    def energy_uj(self):
        # cumulative energy counter (µJ); the window derives live power draw from its delta.
        # energy1 = whole card, energy2 = GPU package.
        return _int(os.path.join(self.hwmon, "energy1_input")) if self.hwmon else None

    def energy2_uj(self):
        return _int(os.path.join(self.hwmon, "energy2_input")) if self.hwmon else None

    def throttle_flags(self):
        # freq0/throttle/reason_* are 0/1 flags (thermal, pl1/pl2/pl4, prochot, vr_tdc…)
        tdir = os.path.join(self.card or "", "device/tile0/gt0/freq0/throttle")
        out = {}
        for f in glob.glob(os.path.join(tdir, "reason_*")):
            out[os.path.basename(f)[len("reason_"):]] = (_read(f) == "1")
        return out

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

    def power_profile(self):
        # freq0/power_profile e.g. "[base] power_saving" — bracketed token is the active one
        raw = _read(os.path.join(self.card or "", "device/tile0/gt0/freq0/power_profile"))
        if not raw:
            return None
        opts, cur = [], None
        for tok in raw.split():
            name = tok.strip("[]")
            opts.append(name)
            if tok.startswith("["):
                cur = name
        return {"current": cur, "options": opts} if opts else None

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
                "vram": self.temps_where(True), "energy": self.energy_uj(),
                "energy2": self.energy2_uj(), "throttle_flags": self.throttle_flags(),
                "profile": self.power_profile()}

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

    def oc_node(self):
        return os.path.join(self.card or "_", "device/tile0/gt0/oc/vf_curve")

    @property
    def oc_available(self):
        return os.path.exists(self.oc_node())

    def read_vf_curve(self):
        # 85 "<index> <voltage_mV>" lines -> list of mV by point index. [] if unavailable.
        raw = _read(self.oc_node())
        out = []
        for line in (raw or "").splitlines():
            f = line.split()
            if len(f) == 2 and f[0].lstrip("-").isdigit() and f[1].lstrip("-").isdigit():
                out.append(int(f[1]))
        return out

    def oc_offset(self):
        # persisted VF-curve offset (mV) from /etc/xe-gpu-oc.conf; 0 = stock, None = custom curve
        for line in (_read("/etc/xe-gpu-oc.conf") or "").splitlines():
            if line.strip().startswith("VOLTAGE_OFFSET="):
                v = line.split("=", 1)[1].strip()
                if v == "custom":
                    return None
                try:
                    return int(v)
                except ValueError:
                    return 0
        return 0

    def mem_node(self):
        return os.path.join(self.card or "_", "device/tile0/gt0/oc/mem_speed")

    @property
    def mem_available(self):
        return os.path.exists(self.mem_node())

    def read_mem_speed(self):
        return _int(self.mem_node())   # Mbps, or None

    def temp_node(self):
        return os.path.join(self.card or "_", "device/tile0/gt0/oc/temp_limit")

    @property
    def temp_available(self):
        return os.path.exists(self.temp_node())

    def read_temp_limit(self):
        return _int(self.temp_node())  # degC, or None


PRESETS = {
    "Silent":     [[40, 0], [55, 45], [70, 100], [82, 170], [90, 255]],
    "Balanced":   [[40, 60], [55, 95], [65, 140], [75, 195], [85, 255]],
    "Cool/Loud":  [[35, 90], [50, 150], [65, 205], [80, 255]],
}


BAND_CLASSES = ("tb0", "tb1", "tb2", "tb3", "tb4", "tb5", "tb6")
_BAND_RGB = {"tb0": (0.13, 0.76, 0.65), "tb1": (0.18, 0.82, 0.50), "tb2": (0.60, 0.82, 0.18),
             "tb3": (0.96, 0.76, 0.07), "tb4": (1.0, 0.62, 0.27), "tb5": (1.0, 0.42, 0.24),
             "tb6": (1.0, 0.30, 0.30)}


def temp_style(c, crit):
    """Graduated temperature colour by headroom to the crit limit (falls back to an
    absolute scale if crit is unknown). Returns (band_class, rgb)."""
    head = (crit - c) if crit else (100 - c)
    for thresh, cls in ((45, "tb0"), (35, "tb1"), (27, "tb2"), (20, "tb3"), (13, "tb4"), (7, "tb5")):
        if head >= thresh:
            return cls, _BAND_RGB[cls]
    return "tb6", _BAND_RGB["tb6"]


SPARK_MAXPTS = 120        # sparkline history capacity — oldest drops as new samples arrive

CONFIG_DIR = os.path.expanduser("~/.config/xe-gpu-gui")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _num(v):
    return str(v) if v is not None else "—"


def _temp_label(lbl):
    return {"pkg": "GPU (pkg)", "vram": "VRAM", "mctrl": "Mem ctrl", "pcie": "PCIe"}.get(
        lbl, "VRAM ch " + lbl.rsplit("_", 1)[-1] if lbl.startswith("vram_ch_") else lbl)


class Metric:
    def __init__(self, mid, label, unit, compute, spark=False, fixed=None, default=True,
                 group="GPU", core=False):
        self.id = mid; self.label = label; self.unit = unit; self.compute = compute
        self.spark = spark; self.fixed = fixed; self.default = default; self.group = group
        self.core = core          # core metrics are always shown; others are filter-toggled


def _temp_metric(lbl):
    def f(d):
        t = d["temp_by_label"].get(lbl)
        if not t:
            return {"text": "—"}
        st, rgb = temp_style(t["c"], t["crit"])
        return {"text": str(t["c"]), "val": t["c"], "state": st, "rgb": rgb,
                "sub": (f"limit {t['crit']}°C" if t.get("crit") else None)}
    return f


def _temp_pct(d):
    t = d["temp_by_label"].get("pkg")
    if not t or not t.get("crit"):
        return {"text": "—"}
    p = round(t["c"] / t["crit"] * 100)
    st, rgb = temp_style(t["c"], t["crit"])
    return {"text": str(p), "val": p, "state": st, "rgb": rgb}


def _limit(flag_keys):
    def f(d):
        on = any((d.get("throttle_flags") or {}).get(k) for k in flag_keys)
        return {"text": "yes", "state": "tb5", "rgb": _BAND_RGB["tb5"]} if on else {"text": "no"}
    return f


def build_metrics(sample):
    """Live METRICS (values that change) — core ones are always shown, the rest are
    toggled via the Metrics filter. Names follow Intel Arc Control where a matching
    reading exists in xe sysfs. Fixed limits/config live in the Specifications section."""
    rp0 = (sample.get("clocks") or {}).get("rp0") or 2400
    return [
        # --- core (always shown) ---
        Metric("freq", "GPU Frequency", "MHz",
               lambda d: {"text": _num(d["clocks"].get("cur")), "val": d["clocks"].get("cur")},
               spark=True, fixed=(0, rp0), core=True),
        Metric("power_card", "GPU Card Power", "W",
               lambda d: {"text": (f"{d['draw_card']:.0f}" if d.get("draw_card") is not None else "—"),
                          "val": d.get("draw_card")}, spark=True, core=True),
        Metric("temp_gpu", "GPU Temperature", "°C", _temp_metric("pkg"),
               spark=True, fixed=(20, 110), core=True),
        Metric("fan", "GPU Fan Speed", "rpm",
               lambda d: {"text": _num(d["fan"].get("rpm")), "val": d["fan"].get("rpm")},
               spark=True, core=True),
        # --- optional (filter, hidden by default) ---
        Metric("freq_act", "GPU Actual Frequency", "MHz",
               lambda d: {"text": _num(d["clocks"].get("act")), "val": d["clocks"].get("act")},
               spark=True, fixed=(0, rp0), default=False, group="Clocks"),
        Metric("power_gpu", "GPU Power", "W",
               lambda d: {"text": (f"{d['draw_pkg']:.0f}" if d.get("draw_pkg") is not None else "—"),
                          "val": d.get("draw_pkg")}, spark=True, default=False, group="Power"),
        Metric("power_pct", "GPU Power Percent", "%",
               lambda d: ({"text": str(round(d["draw_card"] / d["power"]["cap_w"] * 100)),
                           "val": round(d["draw_card"] / d["power"]["cap_w"] * 100)}
                          if d.get("draw_card") is not None and d["power"].get("cap_w") else {"text": "—"}),
               spark=True, fixed=(0, 100), default=False, group="Power"),
        Metric("fan_pct", "GPU Fan Duty", "%",
               lambda d: {"text": (str(round((d["fan"].get("duty") or 0) / 255 * 100))
                                   if d["fan"].get("duty") is not None else "—"),
                          "val": (round((d["fan"].get("duty") or 0) / 255 * 100)
                                  if d["fan"].get("duty") is not None else None)},
               spark=True, fixed=(0, 100), default=False, group="Fan"),
        Metric("temp_pct", "GPU Temperature Percent", "%", _temp_pct,
               spark=True, fixed=(0, 100), default=False, group="Temperature"),
        Metric("temp_vram", "VRAM Temperature", "°C", _temp_metric("vram"),
               spark=True, fixed=(20, 110), default=False, group="Temperature"),
        Metric("lim_power", "GPU Power Limited", "", _limit(("pl1", "pl2", "pl4")),
               default=False, group="Limit indicators"),
        Metric("lim_temp", "GPU Temperature Limited", "", _limit(("thermal",)),
               default=False, group="Limit indicators"),
        Metric("lim_volt", "GPU Voltage Limited", "", _limit(("vr_tdc",)),
               default=False, group="Limit indicators"),
    ]


class MetricTile(Gtk.Box):
    """A dashboard metric card: label, big value + unit, a sub-line, and a live
    scrolling sparkline of recent history — the Windows-style live metric panel."""
    def __init__(self, label, unit="", spark=True, fixed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.add_css_class("mtile"); self.set_hexpand(True)
        self.hist = collections.deque(maxlen=SPARK_MAXPTS)
        self._fixed = fixed              # (lo, hi) fixed y-range, or None = autoscale
        self._rgb = (0.21, 0.52, 0.89)
        self.lbl = Gtk.Label(label=label.upper(), xalign=0); self.lbl.add_css_class("mlabel")
        self.append(self.lbl)
        vr = Gtk.Box(spacing=4)
        self.val = Gtk.Label(label="—", xalign=0); self.val.add_css_class("mvalue")
        vr.append(self.val)
        if unit:
            u = Gtk.Label(label=unit, xalign=0, valign=Gtk.Align.END); u.add_css_class("munit")
            vr.append(u)
        self.append(vr)
        self.sub = Gtk.Label(xalign=0); self.sub.add_css_class("msub"); self.sub.set_visible(False)
        self.append(self.sub)
        if spark:
            self.area = Gtk.DrawingArea(hexpand=True); self.area.set_content_height(30)
            self.area.set_draw_func(self._draw)
            self.append(self.area)
        else:
            self.area = None

    def update(self, text, spark_val=None, rgb=None, state=None, sub=None):
        self.val.set_text(text)
        if rgb is not None:
            self._rgb = rgb
        for cc in BAND_CLASSES:
            self.val.remove_css_class(cc); self.remove_css_class(cc)
        if state:
            self.val.add_css_class(state); self.add_css_class(state)
        if sub is not None:
            self.sub.set_text(sub); self.sub.set_visible(bool(sub))
        if spark_val is not None and self.area is not None:
            self.hist.append(spark_val); self.area.queue_draw()

    def _draw(self, area, cr, w, h, *_):
        if len(self.hist) < 2:
            return
        r, g, b = self._rgb
        lo, hi = self._fixed or (min(self.hist), max(self.hist))
        if hi <= lo:
            hi = lo + 1
        n = len(self.hist)
        cap = self.hist.maxlen or n
        step = w / max(cap - 1, 1)
        # newest sample pinned at the right edge; the trace grows leftward and, once the
        # history is full, scrolls right-to-left as the oldest points drop off.
        X = lambda i: w - (n - 1 - i) * step                 # noqa: E731
        Y = lambda v: h - 2 - (v - lo) / (hi - lo) * (h - 4)  # noqa: E731
        cr.move_to(X(0), h)
        for i, v in enumerate(self.hist):
            cr.line_to(X(i), Y(v))
        cr.line_to(X(n - 1), h); cr.close_path()
        cr.set_source_rgba(r, g, b, 0.15); cr.fill()
        cr.set_line_width(1.7); cr.set_source_rgba(r, g, b, 0.92)
        for i, v in enumerate(self.hist):
            (cr.line_to if i else cr.move_to)(X(i), Y(v))
        cr.stroke()


HELPER_PATHS = {"xe-fan-curve": "/usr/local/bin/xe-fan-curve",
                "xe-gpu-tune": "/usr/local/bin/xe-gpu-tune",
                "xe-gpu-oc": "/usr/local/bin/xe-gpu-oc",
                "xe-gpu-stress": "/usr/local/bin/xe-gpu-stress"}


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
                               "Load the card's stock fan curve into the editor (press Apply to write it)",
                               self._stock, label="Stock"))
        bar.append(icon_button("power-profile-balanced-symbolic",
                               "Auto: hand the fan back to the card's stock auto table",
                               lambda *_: run_priv(["xe-fan-curve", "auto"], self.window,
                                                   self.window.refresh), label="Auto"))
        bar.append(icon_button("power-profile-performance-symbolic",
                               "Max: run the fan at full speed",
                               lambda *_: run_priv(["xe-fan-curve", "max"], self.window,
                                                   self.window.refresh), label="Max"))
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
                  "X = GPU temp °C, Y = fan speed %. The dashed line is the current package temp. "
                  "Apply writes it as a manual curve; Auto hands back to the stock table; Max runs "
                  "full speed.",
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
        i = self._drag
        lo = pts[i - 1][0] + 1 if i > 0 else TMIN
        hi = pts[i + 1][0] - 1 if i < len(pts) - 1 else TMAX
        # PWM must be non-decreasing (driver rejects a curve where speed drops as temp rises)
        lop = pts[i - 1][1] if i > 0 else 0
        hip = pts[i + 1][1] if i < len(pts) - 1 else 255
        pts[i][0] = int(max(lo, min(hi, t)))
        pts[i][1] = int(max(lop, min(hip, round(p))))
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
        # LOAD the card's stock curve into the editor (does not apply — press Apply to write it).
        # Reading stock needs a privileged mode-flip that restores your current curve afterwards.
        def work():
            line = None
            try:
                r = subprocess.run(["pkexec", HELPER_PATHS["xe-fan-curve"], "stock-read"],
                                   capture_output=True, text=True, timeout=25)
                line = next((l for l in r.stdout.splitlines() if l.startswith("STOCK:")), None)
            except Exception:
                line = None
            GLib.idle_add(self._load_stock, line)
        threading.Thread(target=work, daemon=True).start()

    def _load_stock(self, line):
        pts = []
        if line:
            for pr in line[len("STOCK:"):].split():
                try:
                    t, p = pr.split(":"); pts.append([int(t), int(p)])
                except ValueError:
                    pass
        if not pts:
            self.window.toast("Couldn't read the stock curve (cancelled?)")
            return False
        while len(pts) > 2 and pts[-1][1] == pts[-2][1]:   # trim trailing pad points (same pwm)
            pts.pop()
        self.points = pts
        self.area.queue_draw()
        self._mark()
        self.window.toast("Loaded the stock curve — press Apply to write it")
        return False

    def _apply(self, *_):
        pts = sorted(self.points)
        args = ["xe-fan-curve", "set"] + [f"{int(t)}:{int(p)}" for t, p in pts]
        run_priv(args, self.window)
        self.applied = [list(p) for p in pts]
        self._mark()
        self.window.toast(f"Applying fan curve ({len(pts)} points)…")


# ---------------------------------------------------------------- overclock
VMIN_MV, VMAX_MV = 400, 1200      # OC voltage clamp (matches xe-gpu-oc / xe_gt_oc)
STRESS_SECS = 60                  # stability-test load duration

# Overclock preset profiles (offset-based, conservative). Each loads the sliders
# (offset mode) — nothing is written until you press Apply. mem/temp only apply if
# the card exposes them. Voltage is always clamped monotonic + to the voltage limit.
OC_PRESETS = {
    "Stock":       dict(off=0,   vlim=1200, temp=100, mem=19.0),
    "Efficient":   dict(off=-50, vlim=1050, temp=85,  mem=19.0),   # undervolt, cool/quiet
    "Balanced":    dict(off=-25, vlim=1100, temp=95,  mem=19.0),   # mild undervolt
    "Performance": dict(off=25,  vlim=1200, temp=100, mem=20.0),   # slight overvolt + faster VRAM
}


def volt_color(delta):
    """Curve colour by voltage delta vs stock: green undervolt → blue neutral →
    amber → red as overvolt grows. Returns an (r, g, b) tuple in 0..1."""
    if delta <= -6:
        return (0.16, 0.70, 0.42)      # undervolt — green
    if delta >= 45:
        return (0.88, 0.16, 0.16)      # heavy overvolt — red
    if delta >= 6:
        return (0.92, 0.60, 0.10)      # overvolt — amber
    return (0.21, 0.52, 0.89)          # neutral — accent blue


class SliderField:
    """One aligned form row: [icon] [label ⓘ] [====slider====] [spin] [unit].
    The slider and spin share a single Adjustment, so they stay in sync for free.
    A hoverable info (ⓘ) icon carries the full description."""
    def __init__(self, grid, row, icon, label, lo, hi, step, value, unit,
                 digits=0, tip=None, on_change=None):
        self.adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                                  page_increment=step * 5, value=value)
        img = Gtk.Image(icon_name=icon); img.add_css_class("field-icon")
        self.label = Gtk.Label(label=label, xalign=0); self.label.add_css_class("field-label")
        labelbox = Gtk.Box(spacing=5); labelbox.append(self.label)
        if tip:
            img.set_tooltip_text(tip)
            labelbox.append(info_icon(tip))   # explicit ⓘ icon with the description
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                               adjustment=self.adj, hexpand=True, draw_value=False)
        self.scale.add_css_class("oc-scale")
        self.spin = Gtk.SpinButton(adjustment=self.adj, digits=digits, valign=Gtk.Align.CENTER)
        self.spin.set_numeric(True); self.spin.add_css_class("field-spin")
        unit_l = Gtk.Label(label=unit, xalign=0); unit_l.add_css_class("field-unit")
        for col, wdg in enumerate((img, labelbox, self.scale, self.spin, unit_l)):
            grid.attach(wdg, col, row, 1, 1)
        if on_change:
            self.adj.connect("value-changed", lambda *_: on_change())

    @property
    def value(self):
        return self.adj.get_value()

    @value.setter
    def value(self, v):
        self.adj.set_value(v)

    def set_enabled(self, on):
        self.scale.set_sensitive(on); self.spin.set_sensitive(on)
        self.label.set_opacity(1.0 if on else 0.45)

    def mark(self, changed):
        (self.label.add_css_class if changed else self.label.remove_css_class)("field-changed")


class VoltageCurveView(Gtk.Box):
    """VF-curve editor with two modes — a uniform voltage *offset*, or a per-point
    *curve* shaped by dragging anchor nodes — plus voltage-limit, power, memory and
    temperature slider/spin rows. Needs the xe_gt_oc patch (oc/vf_curve)."""

    def __init__(self, gpu, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add_css_class("oc-page")
        self.gpu = gpu
        self.window = window
        self.stock = []            # baseline curve (mV per point), applied offset removed
        self.applied = 0           # uniform offset currently on the GPU
        self.applied_curve = None  # custom curve live on the GPU (list mV) or None
        self.mode = "offset"       # "offset" | "curve"
        self.anchor_i = []         # anchor indices into the 85-point curve
        self.anchor_off = []       # per-anchor offset (mV), curve mode
        self._tgt = []             # cached monotonic target curve (mV per point)
        self._drag = None
        self._loading = True

        # ===== top strip: live telemetry (left) + profile manager (right) =====
        top = Gtk.Box(spacing=8); top.add_css_class("mode-row")
        ti = Gtk.Image(icon_name="utilities-system-monitor-symbolic"); ti.add_css_class("field-icon")
        top.append(ti)
        self.telemetry = Gtk.Label(xalign=0, hexpand=True); self.telemetry.add_css_class("dim")
        self.telemetry.set_markup("<span alpha='55%'>reading live stats…</span>")
        top.append(self.telemetry)
        plbl = Gtk.Label(label="Profile"); plbl.add_css_class("field-label")
        top.append(plbl)
        top.append(info_icon("Save the current voltage/memory/temp settings as a named profile, "
                             "then load or delete saved ones. Loading applies immediately."))
        self.prof_names = []
        self.prof_dd = Gtk.DropDown.new_from_strings(["(none saved)"])
        self.prof_dd.set_sensitive(False)
        top.append(self.prof_dd)
        top.append(icon_button("list-add-symbolic", "Save the current settings as a new profile",
                               self._prof_save, label="Save"))
        top.append(icon_button("document-open-symbolic", "Load the selected profile",
                               self._prof_load, label="Load"))
        top.append(icon_button("user-trash-symbolic", "Delete the selected profile", self._prof_delete))
        self.append(top)

        # ===== two-column body: voltage (left) · power / memory / thermal (right) =====
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_size_request(600, -1)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        body.append(left); body.append(right)
        self.append(body)

        # --- Section: Voltage curve (graph + mode toggle + offset/limit) ---
        vsec = card("Voltage curve",
                    "The GPU voltage-frequency curve — each frequency step's voltage (mV). "
                    "Undervolt for efficiency, overvolt for clock headroom. "
                    "X = idle → max, Y = voltage; dashed = stock, solid = your preview.")
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_size_request(560, 260)
        self.area.set_draw_func(self._draw)
        frame = Gtk.Frame(); frame.add_css_class("card2"); frame.set_child(self.area)
        vsec.append(frame)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_begin)
        drag.connect("drag-update", self._on_update)
        drag.connect("drag-end", lambda *_: setattr(self, "_drag", None))
        self.area.add_controller(drag)

        moderow = Gtk.Box(spacing=8); moderow.add_css_class("mode-row")
        micon = Gtk.Image(icon_name="power-profile-balanced-symbolic"); micon.add_css_class("field-icon")
        moderow.append(micon)
        self.chk_curve = Gtk.CheckButton(label="Per-point curve")
        self.chk_curve.set_tooltip_text(
            "Off: shift the whole curve by one uniform voltage offset.\n"
            "On: shape the curve — drag the anchor nodes to set the voltage at each "
            "frequency region independently (a full custom VF curve).")
        self.chk_curve.connect("toggled", self._on_mode)
        moderow.append(self.chk_curve)
        moderow.append(info_icon("Off: one uniform voltage offset shifts the whole curve.\n"
                                 "On: drag the round nodes to shape voltage per frequency region."))
        moderow.append(Gtk.Box(hexpand=True))
        picon = Gtk.Image(icon_name="starred-symbolic"); picon.add_css_class("field-icon")
        moderow.append(picon)
        self.preset = Gtk.DropDown.new_from_strings(["Preset…"] + list(OC_PRESETS))
        self.preset.set_tooltip_text("Load a conservative overclock profile into the sliders "
                                     "(offset mode). Nothing is written until you press Apply.")
        self.preset.connect("notify::selected", self._on_preset)
        moderow.append(self.preset)
        vsec.append(moderow)

        vgrid = Gtk.Grid(column_spacing=12, row_spacing=10)
        self.f_off = SliderField(
            vgrid, 0, "power-profile-power-saver-symbolic", "Voltage offset",
            -150, 150, 5, 0, "mV",
            tip="Shift every point of the VF curve. −mV undervolts (cooler, more efficient); "
                "+mV overvolts (headroom for higher clocks).",
            on_change=self._on_off)
        self.f_vlim = SliderField(
            vgrid, 1, "security-high-symbolic", "Voltage limit",
            800, VMAX_MV, 10, VMAX_MV, "mV",
            tip="Ceiling for the curve's peak voltage — the applied curve is clamped here. "
                "A safety cap on how high voltage may go.",
            on_change=self._on_knob)
        vsec.append(vgrid)
        left.append(vsec)

        # --- Section: Power & clocks ---
        psec = card("Power & clocks",
                    "Board power cap (TDP) plus the GPU clock floor/ceiling and the driver power "
                    "profile. Raise power to sustain higher clocks; lower for cooler/quieter.")
        pgrid = Gtk.Grid(column_spacing=12, row_spacing=10)
        pr = 0
        self.f_pow = None
        cap0 = self.gpu.power().get("cap_w")
        if cap0:
            self.pow_applied = int(cap0)
            self.f_pow = SliderField(
                pgrid, pr, "power-profile-performance-symbolic", "Power limit",
                50, 400, 5, cap0, "W",
                tip="Board power cap (TDP). Higher sustains boost clocks longer; lower runs "
                    "cooler/quieter.", on_change=self._on_knob)
            pr += 1
        cl = self.gpu.clocks()
        clo = cl.get("rpn") or 400
        chi = cl.get("rp0") or 2400
        self.cmin_applied = int(cl.get("min") or clo)
        self.cmax_applied = int(cl.get("max") or chi)
        self.f_cmin = SliderField(
            pgrid, pr, "go-first-symbolic", "Min clock", clo, chi, 50, self.cmin_applied, "MHz",
            tip="Idle clock floor. Lower (e.g. 400) drops idle power/heat; still boosts under load.",
            on_change=self._on_knob)
        pr += 1
        self.f_cmax = SliderField(
            pgrid, pr, "go-last-symbolic", "Max clock", clo, chi, 50, self.cmax_applied, "MHz",
            tip="Clock ceiling under load. Lower for less heat/noise; raise to the hardware max for "
                "peak performance.", on_change=self._on_knob)
        pr += 1
        self.profile_dd = None
        self.prof_applied = None
        prof = self.gpu.power_profile()
        if prof and prof.get("options"):
            gi = Gtk.Image(icon_name="power-profile-balanced-symbolic"); gi.add_css_class("field-icon")
            gl = Gtk.Label(label="Power profile", xalign=0); gl.add_css_class("field-label")
            glbox = Gtk.Box(spacing=5); glbox.append(gl)
            glbox.append(info_icon("Driver power profile: 'power_saving' trims idle draw; "
                                   "'base' is the default balanced profile."))
            self.profile_dd = Gtk.DropDown.new_from_strings(prof["options"])
            if prof.get("current") in prof["options"]:
                self.profile_dd.set_selected(prof["options"].index(prof["current"]))
                self.prof_applied = prof["current"]
            self.profile_dd.set_halign(Gtk.Align.START)
            self.profile_dd.connect("notify::selected", lambda *_: self._on_knob())
            pgrid.attach(gi, 0, pr, 1, 1); pgrid.attach(glbox, 1, pr, 1, 1)
            pgrid.attach(self.profile_dd, 2, pr, 1, 1)
        psec.append(pgrid); right.append(psec)

        # --- Section: Memory ---
        self.f_mem = None
        if self.gpu.mem_available:
            msec = card("Memory",
                        "GDDR6 video-memory data rate. More bandwidth helps memory-bound "
                        "workloads; raise in small steps and run the stability test.")
            mgrid = Gtk.Grid(column_spacing=12, row_spacing=10)
            self.mem_applied = 19000
            self.f_mem = SliderField(
                mgrid, 0, "drive-harddisk-symbolic", "Memory speed",
                14.0, 24.0, 0.1, 19.0, "Gbps", digits=2,
                tip="GDDR6 data rate. Higher = more VRAM bandwidth; raise cautiously.",
                on_change=self._on_knob)
            msec.append(mgrid); right.append(msec)

        # --- Section: Thermal ---
        self.f_temp = None
        if self.gpu.temp_available:
            tsec = card("Thermal",
                        "GPU thermal-throttle target. Raise it to hold boost clocks longer under "
                        "load; lower it to run cooler and quieter.")
            tgrid = Gtk.Grid(column_spacing=12, row_spacing=10)
            self.temp_applied = 100
            self.f_temp = SliderField(
                tgrid, 0, "dialog-warning-symbolic", "Temp limit",
                60, 100, 1, 100, "°C",
                tip="Thermal-throttle target. Higher = more sustained clock before throttling; "
                    "lower = cooler/quieter.", on_change=self._on_knob)
            tsec.append(tgrid); right.append(tsec)

        # --- action bar (full width) ---
        bar = Gtk.Box(spacing=8)
        self.hint = Gtk.Label(xalign=0, hexpand=True); self.hint.add_css_class("dim")
        bar.append(self.hint)
        self._testing = False
        self.test_btn = icon_button(
            "media-playback-start-symbolic",
            f"Run a {STRESS_SECS}s GPU load and watch for instability / throttling "
            "(auto-reverts to stock if it hangs)", self._stress, label="Test")
        bar.append(self.test_btn)
        bar.append(icon_button("view-refresh-symbolic", "Re-read everything from the GPU",
                               lambda *_: self._load(), label="Reload"))
        bar.append(icon_button("edit-undo-symbolic", "Restore stock curve + memory + temp",
                               self._reset, label="Reset"))
        self.apply_btn = icon_button("emblem-ok-symbolic",
                                     "Apply changed settings — asks for authorization",
                                     self._apply, label="Apply", css="suggested-action")
        bar.append(self.apply_btn)
        self.append(bar)

        note = Gtk.Label(
            label="X = frequency step (idle → max), Y = voltage. Dashed = stock, solid = your "
                  "preview. In curve mode drag the round nodes to shape voltage per region; the "
                  "red line marks your voltage limit. Apply saves your choice (re-applied at boot).",
            xalign=0, wrap=True)
        note.add_css_class("dim")
        self.append(note)
        self._load()

    # ---- mode ----
    def _on_mode(self, chk):
        self.mode = "curve" if chk.get_active() else "offset"
        if self.mode == "curve":
            base = int(self.f_off.value)          # seed anchors from the uniform offset
            self.anchor_off = [base for _ in self.anchor_i]
        self.f_off.set_enabled(self.mode == "offset")
        self._hint(); self.area.queue_draw(); self._mark()

    def _on_preset(self, dd, _):
        i = dd.get_selected()
        if i <= 0:
            return
        name = list(OC_PRESETS)[i - 1]
        p = OC_PRESETS[name]
        if self.chk_curve.get_active():          # presets are offset-based
            self.chk_curve.set_active(False)     # -> _on_mode sets mode + enables offset
        self.f_vlim.value = p["vlim"]
        self.f_off.value = p["off"]
        if self.f_temp is not None:
            self.f_temp.value = p["temp"]
        if self.f_mem is not None:
            self.f_mem.value = p["mem"]
        dd.set_selected(0)
        self._hint(); self.area.queue_draw(); self._mark()
        self.window.toast(f"Loaded “{name}” profile — press Apply to write it")

    def _on_off(self):
        self._hint(); self.area.queue_draw(); self._mark()

    def _on_knob(self):
        self._hint(); self.area.queue_draw(); self._mark()

    # ---- live telemetry (fed by the window's 2s refresh) ----
    def set_telemetry(self, mhz, temp_c, rpm):
        parts = []
        if mhz is not None:
            parts.append(f"{mhz} MHz")
        if temp_c is not None:
            parts.append(f"{temp_c}°C")
        if rpm is not None:
            parts.append(f"{rpm} rpm")
        self.telemetry.set_markup("  ·  ".join(f"<b>{p}</b>" for p in parts) if parts else "—")

    # ---- profiles ----
    def _prof_refresh(self):
        def work():
            try:
                out = subprocess.run([HELPER_PATHS["xe-gpu-oc"], "profile", "names"],
                                     capture_output=True, text=True, timeout=5).stdout
            except Exception:
                out = ""
            names = [l.strip() for l in out.splitlines() if l.strip()]
            GLib.idle_add(self._prof_set, names)
        threading.Thread(target=work, daemon=True).start()

    def _prof_set(self, names):
        self.prof_names = names
        self.prof_dd.set_model(Gtk.StringList.new(names if names else ["(none saved)"]))
        self.prof_dd.set_sensitive(bool(names))
        return False

    def _prof_selected(self):
        if not self.prof_names:
            return None
        i = self.prof_dd.get_selected()
        return self.prof_names[i] if 0 <= i < len(self.prof_names) else None

    def _prof_save(self, *_):
        dlg = Adw.MessageDialog(transient_for=self.window, heading="Save profile",
                                body="Name this overclock profile (voltage / memory / temp):")
        entry = Gtk.Entry(placeholder_text="e.g. daily, gaming, quiet", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save"); dlg.set_close_response("cancel")

        def on_resp(d, resp):
            name = entry.get_text().strip()
            if resp == "save" and name:
                run_priv(["xe-gpu-oc", "profile", "save", name], self.window,
                         lambda: (self._prof_refresh(), self.window.toast(f"Saved profile “{name}”")))
        dlg.connect("response", on_resp)
        dlg.present()

    def _prof_load(self, *_):
        name = self._prof_selected()
        if not name:
            self.window.toast("No profile selected"); return
        run_priv(["xe-gpu-oc", "profile", "load", name], self.window,
                 lambda: (setattr(self, "applied", 0), setattr(self, "applied_curve", None),
                          self._load(), self.window.toast(f"Loaded profile “{name}”")))

    def _prof_delete(self, *_):
        name = self._prof_selected()
        if not name:
            self.window.toast("No profile selected"); return
        run_priv(["xe-gpu-oc", "profile", "delete", name], self.window,
                 lambda: (self._prof_refresh(), self.window.toast(f"Deleted profile “{name}”")))

    # ---- data ----
    def _load(self, *_):
        self._loading = True
        self.hint.set_text("reading…"); self.area.queue_draw()

        def work():
            data = self.gpu.read_vf_curve()
            GLib.idle_add(self._loaded, data)
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, data):
        self._loading = False
        off = self.gpu.oc_offset()               # int = uniform offset, None = custom curve
        self.applied = off if isinstance(off, int) else 0
        if data:
            self.anchor_i = self._anchors(len(data))
            if isinstance(off, int):
                self.stock = [v - self.applied for v in data]   # baseline = live − offset
                self.applied_curve = None
                self.mode = "offset"
                self.chk_curve.set_active(False)
                self.anchor_off = [self.applied for _ in self.anchor_i]
            else:
                # a custom curve is live; take it as the shown baseline (flat anchors)
                self.stock = list(data)
                self.applied_curve = list(data)
                self.mode = "curve"
                self.chk_curve.set_active(True)
                self.anchor_off = [0 for _ in self.anchor_i]
            self.f_off.value = self.applied
            self.f_off.set_enabled(self.mode == "offset")
        if self.f_mem is not None:
            mbps = self.gpu.read_mem_speed()
            if mbps:
                self.mem_applied = mbps; self.f_mem.value = mbps / 1000.0
        if self.f_temp is not None:
            degc = self.gpu.read_temp_limit()
            if degc:
                self.temp_applied = degc; self.f_temp.value = degc
        if self.f_pow is not None:
            cap = self.gpu.power().get("cap_w")
            if cap:
                self.pow_applied = int(cap); self.f_pow.value = cap
        cl = self.gpu.clocks()
        if cl.get("min"):
            self.cmin_applied = int(cl["min"]); self.f_cmin.value = cl["min"]
        if cl.get("max"):
            self.cmax_applied = int(cl["max"]); self.f_cmax.value = cl["max"]
        if self.profile_dd is not None:
            prof = self.gpu.power_profile()
            opts = (prof or {}).get("options") or []
            if prof and prof.get("current") in opts:
                self.prof_applied = prof["current"]
                self.profile_dd.set_selected(opts.index(prof["current"]))
        self._prof_refresh()
        self._hint(); self.area.queue_draw(); self._mark()
        return False

    def _prof_sel(self):
        if self.profile_dd is None:
            return None
        it = self.profile_dd.get_selected_item()
        return it.get_string() if it else None

    def _anchors(self, n):
        k = min(8, max(2, n - 1))
        return sorted({round(i * (n - 1) / k) for i in range(k + 1)})

    # ---- curve math ----
    def _off_at(self, i):
        if self.mode == "offset":
            return self.f_off.value
        xs, ys = self.anchor_i, self.anchor_off
        if not xs:
            return 0
        if i <= xs[0]:
            return ys[0]
        if i >= xs[-1]:
            return ys[-1]
        for a in range(len(xs) - 1):
            if xs[a] <= i <= xs[a + 1]:
                t = (i - xs[a]) / max(xs[a + 1] - xs[a], 1)
                return ys[a] + t * (ys[a + 1] - ys[a])
        return 0

    def _vlim(self):
        return int(self.f_vlim.value)

    def _recompute(self):
        # Build the monotonic (non-decreasing) target curve the hardware will accept.
        # Voltage rises with frequency; PCODE pins any point that dips below its
        # predecessor (and the top points sit on the fixed Vmax rail), so mirror that
        # here — an honest preview of exactly what Apply will land.
        tgt, prev, vlim = [], VMIN_MV, self._vlim()
        for i in range(len(self.stock)):
            v = max(VMIN_MV, min(vlim, self.stock[i] + self._off_at(i)))
            v = max(v, prev)
            tgt.append(int(round(v))); prev = v
        self._tgt = tgt

    def _preview(self, i):
        if not self._tgt:
            self._recompute()
        return self._tgt[i] if i < len(self._tgt) else int(self.stock[i])

    def _applied_curve(self):
        n = len(self.stock)
        if self.applied_curve is not None:
            return [int(self.applied_curve[i]) if i < len(self.applied_curve)
                    else int(self.stock[i]) for i in range(n)]
        return [max(VMIN_MV, min(VMAX_MV, int(self.stock[i] + self.applied))) for i in range(n)]

    def _target_curve(self):
        if not self._tgt:
            self._recompute()
        return list(self._tgt)

    def _vf_changed(self):
        return bool(self.stock) and self._target_curve() != self._applied_curve()

    # ---- hint / pending state ----
    def _hint(self):
        if not self.stock:
            self.hint.set_text(""); return
        self._recompute()
        tgt = self._target_curve()
        extra = ""
        if self.mode == "offset" and int(self.f_off.value):
            o = int(self.f_off.value); extra = f"  ({'+' if o > 0 else ''}{o} mV)"
        self.hint.set_text(f"{len(tgt)} pts · {min(tgt)}–{max(tgt)} mV · {self.mode}{extra}")

    def _mark(self):
        changed = self._vf_changed()
        if self.f_pow is not None:
            changed |= int(self.f_pow.value) != int(self.pow_applied)
        if self.f_mem is not None:
            changed |= int(self.f_mem.value * 1000) != int(self.mem_applied)
        if self.f_temp is not None:
            changed |= int(self.f_temp.value) != int(self.temp_applied)
        changed |= int(self.f_cmin.value) != int(self.cmin_applied)
        changed |= int(self.f_cmax.value) != int(self.cmax_applied)
        if self.profile_dd is not None:
            changed |= self._prof_sel() != self.prof_applied
        # voltage offset: semantic colour (green undervolt / amber overvolt)
        lbl = self.f_off.label
        for c in ("field-changed", "field-uv", "field-ov"):
            lbl.remove_css_class(c)
        if self.mode == "offset" and int(self.f_off.value) != int(self.applied):
            o = int(self.f_off.value)
            lbl.add_css_class("field-uv" if o < 0 else "field-ov" if o > 0 else "field-changed")
        # other fields: accent highlight (temp goes red when raised above stock)
        self.f_vlim.mark(self._vlim() < VMAX_MV)
        if self.f_pow is not None:
            self.f_pow.mark(int(self.f_pow.value) != int(self.pow_applied))
        if self.f_mem is not None:
            self.f_mem.mark(int(self.f_mem.value * 1000) != int(self.mem_applied))
        if self.f_temp is not None:
            tl = self.f_temp.label
            for c in ("field-changed", "field-hot"):
                tl.remove_css_class(c)
            if int(self.f_temp.value) != int(self.temp_applied):
                tl.add_css_class("field-hot" if self.f_temp.value >= 95 else "field-changed")
        self.f_cmin.mark(int(self.f_cmin.value) != int(self.cmin_applied))
        self.f_cmax.mark(int(self.f_cmax.value) != int(self.cmax_applied))
        self.apply_btn.set_visible(changed)
        (self.apply_btn.add_css_class if changed else self.apply_btn.remove_css_class)("pulse")

    # ---- apply / reset ----
    def _apply(self, *_):
        queue = []
        if self._vf_changed():
            if self.mode == "offset" and self._vlim() >= VMAX_MV:
                off = int(self.f_off.value)
                queue.append((["xe-gpu-oc", "offset", str(off)],
                              lambda off=off: self._vf_done(off, None),
                              f"voltage {'+' if off > 0 else ''}{off} mV"))
            elif self.mode == "offset":
                off = int(self.f_off.value)
                queue.append((["xe-gpu-oc", "offset", str(off), str(self._vlim())],
                              lambda off=off: self._vf_done(off, None),
                              f"voltage {'+' if off > 0 else ''}{off} mV ≤{self._vlim()}"))
            else:
                tgt = self._target_curve()
                pairs = [f"{i}:{tgt[i]}" for i in range(len(tgt))]
                queue.append((["xe-gpu-oc", "curve"] + pairs,
                              lambda tgt=tgt: self._vf_done(None, tgt),
                              "custom curve"))
        # power + clocks + profile all go through xe-gpu-tune — batch into ONE call
        # (one auth prompt) instead of a separate pkexec per knob.
        tune = ["xe-gpu-tune", "set"]; tune_oks = []; tune_lbls = []
        if self.f_pow is not None and int(self.f_pow.value) != int(self.pow_applied):
            wp = int(self.f_pow.value); tune += ["--power-w", str(wp)]
            tune_oks.append(lambda wp=wp: setattr(self, "pow_applied", wp)); tune_lbls.append(f"power {wp} W")
        if int(self.f_cmin.value) != int(self.cmin_applied):
            wv = int(self.f_cmin.value); tune += ["--clk-min", str(wv)]
            tune_oks.append(lambda wv=wv: setattr(self, "cmin_applied", wv)); tune_lbls.append(f"min {wv} MHz")
        if int(self.f_cmax.value) != int(self.cmax_applied):
            wv = int(self.f_cmax.value); tune += ["--clk-max", str(wv)]
            tune_oks.append(lambda wv=wv: setattr(self, "cmax_applied", wv)); tune_lbls.append(f"max {wv} MHz")
        if self.profile_dd is not None and self._prof_sel() != self.prof_applied:
            wv = self._prof_sel(); tune += ["--profile", wv]
            tune_oks.append(lambda wv=wv: setattr(self, "prof_applied", wv)); tune_lbls.append(f"profile {wv}")
        if len(tune) > 2:
            queue.append((tune, lambda oks=tune_oks: [f() for f in oks], ", ".join(tune_lbls)))
        if self.f_mem is not None:
            wm = int(self.f_mem.value * 1000)
            if wm != int(self.mem_applied):
                queue.append((["xe-gpu-oc", "mem", str(wm)],
                              lambda wm=wm: setattr(self, "mem_applied", wm),
                              f"memory {wm / 1000:.2f} Gbps"))
        if self.f_temp is not None:
            wt = int(self.f_temp.value)
            if wt != int(self.temp_applied):
                queue.append((["xe-gpu-oc", "temp", str(wt)],
                              lambda wt=wt: setattr(self, "temp_applied", wt),
                              f"temp {wt}°C"))
        if not queue:
            return

        def run_next(i):
            if i >= len(queue):
                self._mark(); return
            args, on_ok, _ = queue[i]
            run_priv(args, self.window, lambda: (on_ok(), run_next(i + 1)))
        run_next(0)
        self.window.toast("Applying " + ", ".join(q[2] for q in queue) + " (saved)…")

    def _vf_done(self, off, curve):
        if off is not None:
            self.applied = off; self.applied_curve = None
        if curve is not None:
            self.applied_curve = list(curve)
        self._hint()

    def _reset(self, *_):
        def after_oc():
            self.applied = 0; self.applied_curve = None
            run_priv(["xe-gpu-tune", "reset"], self.window, self._load)   # then power/clocks/profile
        run_priv(["xe-gpu-oc", "reset"], self.window, after_oc)
        self.window.toast("Restoring stock curve + power/clocks + memory + temp…")

    # ---- stability test ----
    def _stress(self, *_):
        if self._testing:
            return
        self._testing = True
        self.test_btn.set_sensitive(False); self.apply_btn.set_sensitive(False)
        self.hint.set_text("stability test starting…")
        self.window.toast(f"Stability test: {STRESS_SECS}s GPU load (fan → max)…")
        # run via pkexec with --fan-guard: root ramps the fan to max + restores it, and
        # runs the workload as us (passing our session env so it can open the display).
        env = os.environ
        cmd = ["pkexec", HELPER_PATHS["xe-gpu-stress"], str(STRESS_SECS), "--fan-guard",
               "--user", env.get("USER") or env.get("LOGNAME") or "",
               "--display", env.get("DISPLAY", ""),
               "--wayland", env.get("WAYLAND_DISPLAY", ""),
               "--runtime", env.get("XDG_RUNTIME_DIR", "")]

        def work():
            summary = {}
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as e:
                GLib.idle_add(self._stress_done, {"STATUS": "error", "_err": str(e)}); return
            for line in p.stdout:
                line = line.strip()
                if line.startswith("PROGRESS"):
                    f = line.split()
                    if len(f) == 4:
                        GLib.idle_add(self._stress_progress, f[1], f[2], f[3])
                elif "=" in line:
                    k, v = line.split("=", 1); summary[k] = v
            p.wait()
            if p.returncode == 126 and "STATUS" not in summary:   # pkexec auth dismissed
                summary["STATUS"] = "cancelled"
            GLib.idle_add(self._stress_done, summary)
        threading.Thread(target=work, daemon=True).start()

    def _stress_progress(self, sec, mhz, tc):
        self.hint.set_text(f"testing… {sec}s / {STRESS_SECS}s · {mhz} MHz · {tc}°C")
        return False

    def _stress_done(self, s):
        self._testing = False
        self.test_btn.set_sensitive(True); self.apply_btn.set_sensitive(True)
        st = s.get("STATUS", "error")
        mt, mnf, mxf = s.get("MAXTEMP", "?"), s.get("MINFREQ", "?"), s.get("MAXFREQ", "?")
        if st == "no_workload":
            self.window.toast("Install glmark2 or vkmark to run a stability test", ms=4000)
        elif st == "cancelled":
            self.window.toast("Stability test cancelled")
        elif st == "error":
            self.window.toast("Stability test could not start")
        elif st == "unstable":
            self.window.toast(f"UNSTABLE under load (peak {mt}°C) — reverting to stock", ms=4500)
            run_priv(["xe-gpu-oc", "reset"], self.window,
                     lambda: (setattr(self, "applied", 0),
                              setattr(self, "applied_curve", None), self._load()))
        elif st == "throttled":
            self.window.toast(f"Stable but throttled — hit {mt}°C "
                              f"(limit {s.get('TEMPLIMIT', '?')}°C)", ms=4500)
        else:
            self.window.toast(f"Stable ✓ — {STRESS_SECS}s, peak {mt}°C, "
                              f"{mnf}–{mxf} MHz, no hang", ms=4500)
        if s.get("NOTE") and st in ("ok", "throttled"):
            self.window.toast(s["NOTE"], ms=5500)
        self._hint()
        return False

    # ---- geometry ----
    def _geo(self):
        return 50, 12, self.area.get_width() - 12, self.area.get_height() - 22

    def _node_px(self, a, geo):
        L, T, R, B = geo
        n = len(self.stock)
        i = self.anchor_i[a]
        x = L + i / max(n - 1, 1) * (R - L)
        tgt = self._target_curve()               # sit the node on the real (monotonic) curve
        v = tgt[i] if i < len(tgt) else int(self.stock[i])
        y = B - (v - VMIN_MV) / (VMAX_MV - VMIN_MV) * (B - T)
        return x, y

    # ---- drag (curve mode) ----
    def _on_begin(self, g, x, y):
        self._drag = None
        if self.mode != "curve" or not self.stock:
            return
        geo = self._geo()
        for a in range(len(self.anchor_i)):
            px, py = self._node_px(a, geo)
            if (px - x) ** 2 + (py - y) ** 2 <= 16 ** 2:
                self._drag = a; break

    def _on_update(self, g, dx, dy):
        if self._drag is None:
            return
        _, sx, sy = g.get_start_point()
        L, T, R, B = self._geo()
        v = VMIN_MV + (B - (sy + dy)) / max(B - T, 1) * (VMAX_MV - VMIN_MV)
        v = max(VMIN_MV, min(VMAX_MV, v))
        a = self._drag
        self.anchor_off[a] = int(round(v - self.stock[self.anchor_i[a]]))
        self._hint(); self.area.queue_draw(); self._mark()

    # ---- drawing ----
    def _draw(self, area, cr, w, h, *_):
        L, T, R, B = 50, 12, w - 12, h - 22
        fg = area.get_color(); r, g, b = fg.red, fg.green, fg.blue
        cr.select_font_face("sans", 0, 0); cr.set_font_size(10); cr.set_line_width(1)
        for mv in range(VMIN_MV, VMAX_MV + 1, 200):
            y = B - (mv - VMIN_MV) / (VMAX_MV - VMIN_MV) * (B - T)
            cr.set_source_rgba(r, g, b, 0.10); cr.move_to(L, y); cr.line_to(R, y); cr.stroke()
            cr.set_source_rgba(r, g, b, 0.45); cr.move_to(8, y + 3); cr.show_text(str(mv))
        cr.set_source_rgba(r, g, b, 0.45)
        cr.move_to(L, B + 14); cr.show_text("idle")
        cr.move_to(R - 24, B + 14); cr.show_text("max")
        if self._loading or not self.stock:
            cr.set_source_rgba(r, g, b, 0.5); cr.move_to(L + 12, (T + B) / 2)
            cr.show_text("reading…" if self._loading else "no OC data (xe_gt_oc patch not loaded)")
            return
        n = len(self.stock)

        def X(i):
            return L + i / max(n - 1, 1) * (R - L)

        def Y(mv):
            mv = max(VMIN_MV, min(VMAX_MV, mv))
            return B - (mv - VMIN_MV) / (VMAX_MV - VMIN_MV) * (B - T)

        # voltage-limit line (red, dashed)
        vlim = self._vlim()
        if vlim < VMAX_MV:
            yl = Y(vlim)
            cr.set_source_rgba(0.88, 0.11, 0.14, 0.85); cr.set_line_width(1.4); cr.set_dash([6, 3])
            cr.move_to(L, yl); cr.line_to(R, yl); cr.stroke(); cr.set_dash([])
            cr.move_to(R - 62, yl - 3); cr.show_text(f"limit {vlim}")

        tgt = self._target_curve()               # monotonic, limit-clamped

        # filled zones under preview, coloured per segment by delta vs stock
        for i in range(1, n):
            c = volt_color(tgt[i] - self.stock[i])
            cr.set_source_rgba(c[0], c[1], c[2], 0.13)
            cr.move_to(X(i - 1), B); cr.line_to(X(i - 1), Y(tgt[i - 1]))
            cr.line_to(X(i), Y(tgt[i])); cr.line_to(X(i), B); cr.close_path(); cr.fill()

        # stock (dashed, dim)
        cr.set_dash([4, 3]); cr.set_line_width(1.4); cr.set_source_rgba(r, g, b, 0.35)
        for i in range(n):
            (cr.line_to if i else cr.move_to)(X(i), Y(self.stock[i]))
        cr.stroke(); cr.set_dash([])

        # preview line, coloured per segment (green undervolt → amber/red overvolt)
        cr.set_line_width(2.6)
        for i in range(1, n):
            c = volt_color(tgt[i] - self.stock[i])
            cr.set_source_rgba(c[0], c[1], c[2], 0.97)
            cr.move_to(X(i - 1), Y(tgt[i - 1])); cr.line_to(X(i), Y(tgt[i])); cr.stroke()

        # anchor nodes (curve mode), tinted by their own delta
        if self.mode == "curve":
            geo = (L, T, R, B)
            for a in range(len(self.anchor_i)):
                px, py = self._node_px(a, geo)
                idx = self.anchor_i[a]
                c = volt_color(tgt[idx] - self.stock[idx])
                cr.set_source_rgba(c[0], c[1], c[2], 1.0); cr.arc(px, py, 6, 0, 6.29); cr.fill()
                cr.set_source_rgba(1, 1, 1, 0.92); cr.arc(px, py, 2.6, 0, 6.29); cr.fill()


# ---------------------------------------------------------------- window
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Arc GPU Dashboard")
        self.set_default_size(1240, 780)
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

        p1 = self.stack.add_titled(self._build_dashboard(), "dash", "Dashboard")
        p1.set_icon_name("utilities-system-monitor-symbolic")

        self.editor = CurveEditor(self.gpu, self)
        curve_wrap = Gtk.Box(margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        curve_wrap.append(self.editor)
        p2 = self.stack.add_titled(curve_wrap, "curve", "Fan Control")
        p2.set_icon_name("power-profile-balanced-symbolic")

        # Overclock tab — only if the xe_gt_oc patch exposes the VF curve
        if self.gpu.oc_available:
            self.oc_view = VoltageCurveView(self.gpu, self)
            self.oc_view.set_margin_start(12); self.oc_view.set_margin_end(12)
            self.oc_view.set_margin_top(12); self.oc_view.set_margin_bottom(12)
            oc_wrap = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            oc_wrap.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            oc_wrap.set_child(self.oc_view)
            p3 = self.stack.add_titled(oc_wrap, "oc", "Overclock")
            p3.set_icon_name("power-profile-performance-symbolic")

        self._reading = False
        self._tick()              # first async snapshot
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def toast(self, msg, ms=2500):
        # timeout=0 keeps Adw from auto-dismissing; we dismiss manually for a precise 2.5s.
        t = Adw.Toast(title=msg, timeout=0)
        self.toasts.add_toast(t)
        GLib.timeout_add(ms, lambda: (t.dismiss(), False)[1])

    def _build_dashboard(self):
        sample = self.gpu.snapshot()
        self.metrics = build_metrics(sample)
        self.metric_by_id = {m.id: m for m in self.metrics}
        self._extras = [m for m in self.metrics if not m.core]
        saved = load_config().get("metrics", {})
        self.visible = {m.id: bool(saved.get(m.id, m.default)) for m in self._extras}
        self.tiles = {}
        self._energy_prev = {}
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self._build_specs(page)                    # fixed values, on top
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); left.set_size_request(360, -1)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        row.append(left); row.append(right)
        self._build_temps(left)                    # Temperatures on the left
        self._build_metrics_card(right)            # live Metrics on the right
        page.append(row)
        self._build_controls(page)                 # power/clock card only when there's no OC tab
        return page

    def _metric_visible(self, child):
        mid = getattr(child.get_child(), "metric_id", None)
        m = self.metric_by_id.get(mid)
        return bool(m and (m.core or self.visible.get(mid, False)))

    def _build_metrics_card(self, parent):
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); c.add_css_class("card2")
        c.set_hexpand(True); c.set_vexpand(True)
        head = Gtk.Box(spacing=6)
        t = Gtk.Label(label="METRICS", xalign=0, hexpand=True); t.add_css_class("section")
        head.append(t); head.append(self._build_filter_button())
        c.append(head)
        self.flow = Gtk.FlowBox(column_spacing=10, row_spacing=10, homogeneous=True,
                                min_children_per_line=2, max_children_per_line=3,
                                selection_mode=Gtk.SelectionMode.NONE)
        self.flow.set_filter_func(self._metric_visible)
        for m in self.metrics:
            tile = MetricTile(m.label, m.unit, spark=m.spark, fixed=m.fixed)
            tile.metric_id = m.id
            self.tiles[m.id] = tile
            self.flow.append(tile)
        c.append(self.flow); parent.append(c)

    def _build_filter_button(self):
        btn = Gtk.MenuButton(tooltip_text="Show/hide optional metrics (saved across launches)")
        btn.set_child(Adw.ButtonContent(icon_name="view-list-symbolic", label="Metrics"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        cap = Gtk.Label(label="Optional metrics (the core four are always shown)",
                        xalign=0, wrap=True, max_width_chars=30); cap.add_css_class("msub")
        box.append(cap)
        row = Gtk.Box(spacing=6, margin_top=4)
        row.append(icon_button("edit-select-all-symbolic", "Show all",
                               lambda *_: self._filter_bulk(True), label="All"))
        row.append(icon_button("edit-clear-all-symbolic", "Hide all",
                               lambda *_: self._filter_bulk(False), label="None"))
        box.append(row)
        self._filter_checks = {}
        lastgroup = None
        for m in self._extras:
            if m.group != lastgroup:
                gl = Gtk.Label(label=m.group.upper(), xalign=0); gl.add_css_class("filter-group")
                box.append(gl); lastgroup = m.group
            cb = Gtk.CheckButton(label=m.label)
            cb.set_active(self.visible.get(m.id, m.default))
            cb.connect("toggled", self._on_filter_toggle, m.id)
            self._filter_checks[m.id] = cb
            box.append(cb)
        fsw = Gtk.ScrolledWindow(propagate_natural_width=True)
        fsw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        fsw.set_max_content_height(420); fsw.set_child(box)
        pop = Gtk.Popover(); pop.set_child(fsw)
        btn.set_popover(pop)
        return btn

    def _on_filter_toggle(self, cb, mid):
        self.visible[mid] = cb.get_active()
        self._persist_filter(); self.flow.invalidate_filter()

    def _filter_bulk(self, val):
        for m in self._extras:
            self.visible[m.id] = val
            self._filter_checks[m.id].set_active(val)
        self._persist_filter(); self.flow.invalidate_filter()

    def _persist_filter(self):
        cfg = load_config(); cfg["metrics"] = self.visible; save_config(cfg)

    def _build_specs(self, parent):
        c = card("Specifications", "Fixed limits & configuration — not live metrics.")
        self.spec_rows = {}
        g = Gtk.Grid(column_spacing=18, row_spacing=6)
        rows = [("device", "Device"), ("cap", "Power cap"), ("limit", "Power limit (I1)"),
                ("clk", "Clock limits"), ("hw", "Hardware range"), ("profile", "Power profile"),
                ("fan", "Fan mode")]
        for i, (key, label) in enumerate(rows):
            col = (i % 2) * 2; r = i // 2
            k = Gtk.Label(label=label, xalign=0); k.add_css_class("dim")
            v = Gtk.Label(label="—", xalign=0); v.add_css_class("cval")
            g.attach(k, col, r, 1, 1); g.attach(v, col + 1, r, 1, 1)
            self.spec_rows[key] = v
        c.append(g); parent.append(c)

    def _build_temps(self, parent):
        c = card("Temperatures", "All sensors the driver exposes. Colour = headroom to the crit limit.")
        c.set_vexpand(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.main_t = {}
        mg = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        for i, name in enumerate(("pkg", "mctrl", "pcie", "vram")):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2); cell.add_css_class("chip")
            lab = Gtk.Label(label=name, xalign=0); lab.add_css_class("clbl")
            val = Gtk.Label(label="—", xalign=0); val.add_css_class("big")
            bar = Gtk.LevelBar(min_value=0, max_value=110, hexpand=True)
            cell.append(lab); cell.append(val); cell.append(bar)
            mg.attach(cell, i % 2, i // 2, 1, 1)
            self.main_t[name] = (cell, val, bar)
        inner.append(mg)
        vh = Gtk.Label(label="VRAM CHANNELS", xalign=0); vh.add_css_class("section"); vh.set_margin_top(2)
        inner.append(vh)
        self.vram_chips = {}
        self.vram_grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        inner.append(self.vram_grid)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_child(inner)
        c.append(sw); parent.append(c)

    def _tc(self, w, cls):
        for x in BAND_CLASSES:
            w.remove_css_class(x)
        w.add_css_class(cls)

    def _build_controls(self, parent):
        # Power/clocks/profile live here ONLY when there's no Overclock tab (no xe_gt_oc
        # patch); with the patch they're consolidated onto the Overclock tab. Fan
        # quick-actions (Auto/Max) + the curve editor live on the Fan Control tab. So on
        # a patched card the Dashboard is monitoring-only — nothing to build here.
        if self.gpu.oc_available:
            return
        c = card("Power & clocks", "Writes run the xe-* helpers via pkexec (asks for your password).")

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
        prof = self.gpu.power_profile()
        self.profile_dd = None
        if prof and prof.get("options"):
            self.profile_dd = Gtk.DropDown.new_from_strings(prof["options"])
            if prof.get("current") in prof["options"]:
                self.profile_dd.set_selected(prof["options"].index(prof["current"]))
            self.profile_dd.connect("notify::selected", lambda *_: self._mark_tune())
            kv(g, 3, "Power profile", self.profile_dd,
               "Driver power profile: 'power_saving' trims idle draw; 'base' is the default.")
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
        prof = None
        if getattr(self, "profile_dd", None) is not None:
            it = self.profile_dd.get_selected_item()
            prof = it.get_string() if it else None
        return (self.sp_pow.get_value(), self.sp_min.get_value(), self.sp_max.get_value(), prof)

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

    def _spin(self, lo, hi, step, val):
        s = Gtk.SpinButton(adjustment=Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                                                     value=val), valign=Gtk.Align.CENTER)
        s.set_numeric(True)
        return s

    def on_apply(self, _b):
        cur = self._tune_vals()
        args = ["xe-gpu-tune", "set"]
        if cur[0] != self.tune_base[0]:
            args += ["--power-w", str(int(cur[0]))]
        if cur[1] != self.tune_base[1]:
            args += ["--clk-min", str(int(cur[1]))]
        if cur[2] != self.tune_base[2]:
            args += ["--clk-max", str(int(cur[2]))]
        if len(cur) > 3 and cur[3] and cur[3] != self.tune_base[3]:
            args += ["--profile", cur[3]]
        if len(args) == 2:      # nothing actually changed
            return
        run_priv(args, self, self.refresh)
        self.tune_base = cur
        self._mark_tune()
        self.toast("Applying " + " ".join(args[2:]).replace("--power-w", "power")
                   .replace("--clk-min", "min").replace("--clk-max", "max").replace("--profile", "profile"))

    def _tc(self, w, cls):
        for x in BAND_CLASSES:
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

    def _power_draw(self, key, energy_uj):
        # live watts from the cumulative energy counter's delta over wall time (per rail)
        if energy_uj is None:
            return None
        now = GLib.get_monotonic_time()          # µs
        prev = self._energy_prev.get(key)
        self._energy_prev[key] = (energy_uj, now)
        if prev is None:
            return None
        de = energy_uj - prev[0]; dt = (now - prev[1]) / 1e6
        if dt <= 0 or de < 0:
            return None
        return (de / 1e6) / dt                    # J / s = W

    def _apply(self, data):
        self._reading = False
        if not data:
            return False
        data["draw_card"] = self._power_draw("card", data.get("energy"))
        data["draw_pkg"] = self._power_draw("pkg", data.get("energy2"))
        data["temp_by_label"] = {t["label"]: t for t in data["mains"] + data["vram"]}
        # --- Specifications (fixed values) ---
        ident = data["id"]; pw = data["power"]; cl = data["clocks"]; prof = data.get("profile") or {}
        self.spec_rows["device"].set_text(f"{ident['card']} · {ident['id']}")
        self.spec_rows["cap"].set_text(f"{pw['cap_w']} W" if pw.get("cap_w") else "—")
        self.spec_rows["limit"].set_text(f"{pw['crit_w']:.0f} W" if pw.get("crit_w") else "—")
        self.spec_rows["clk"].set_text(f"{cl.get('min','—')}–{cl.get('max','—')} MHz")
        self.spec_rows["hw"].set_text(f"{cl.get('rpn','—')}–{cl.get('rp0','—')} MHz")
        self.spec_rows["profile"].set_text(prof.get("current", "—"))
        self.spec_rows["fan"].set_text(data["fan"].get("mode", "—"))
        # --- Metrics (core always, extras only when enabled) ---
        for m in self.metrics:
            tile = self.tiles.get(m.id)
            if tile is None or (not m.core and not self.visible.get(m.id)):
                continue
            try:
                r = m.compute(data)
            except Exception:
                r = {"text": "—"}
            tile.update(r.get("text", "—"), spark_val=(r.get("val") if m.spark else None),
                        rgb=r.get("rgb"), state=r.get("state"), sub=r.get("sub"))
        pkg = data["temp_by_label"].get("pkg")
        if pkg:
            self.editor.cur_pkg = pkg["c"]
        if getattr(self, "oc_view", None) is not None:
            self.oc_view.set_telemetry(cl.get("cur"), pkg["c"] if pkg else None, data["fan"].get("rpm"))
        # --- temperatures grid ---
        mains = {t["label"]: t for t in data["mains"]}
        hottest = max((t["c"] for t in data["mains"] + data["vram"]), default=None)
        for name, (cell, val, bar) in self.main_t.items():
            t = mains.get(name)
            if not t:
                val.set_text("—"); continue
            val.set_text(f"{t['c']}°{' 🔥' if t['c'] == hottest else ''}")
            bar.set_max_value(t["crit"] or 110); bar.set_value(min(t["c"], t["crit"] or 110))
            st, _ = temp_style(t["c"], t["crit"]); self._tc(cell, st); self._tc(val, st)
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
            st, _ = temp_style(t["c"], t["crit"]); self._tc(chip, st)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.exzile.XeGpuDashboard",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    App().run(None)
