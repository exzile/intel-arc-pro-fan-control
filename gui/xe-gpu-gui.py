#!/usr/bin/env python3
# xe-gpu-gui — native GTK4/libadwaita control panel for the Intel Arc (xe) GPU.
# Tabs: Dashboard (live stats + tuning), Fan Curve (graphical draggable editor),
# and Overclock (voltage-frequency curve graph + offset slider — shown when the
# xe_gt_oc patch exposes .../gt0/oc/vf_curve).
# Controls call xe-fan-curve / xe-gpu-tune / xe-gpu-oc via pkexec (polkit prompts
# for writes). Reads are unprivileged sysfs; no kernel poking here.
import os, glob, subprocess, threading, collections, json, math, re, shutil
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
.info { opacity:0.45; }

/* --- dashboard metric tiles (uniform, neutral; only value+line go red when hot) --- */
.mtile  { background: alpha(@window_fg_color,0.05); border-radius: 12px; padding: 9px 11px;
          border: 2px solid alpha(@window_fg_color,0.10);
          transition: transform 140ms ease, box-shadow 160ms ease, border-color 160ms ease; }
.mtile:hover { transform: translateY(-2px); border-color: alpha(@accent_color,0.55);
               box-shadow: 0 5px 16px alpha(#000,0.35); }
.mtile.flash { animation: tileflash 0.7s ease-out; }
@keyframes tileflash {
  0%   { box-shadow: 0 0 0 0 alpha(#ff5c57,0.75); }
  100% { box-shadow: 0 0 0 10px alpha(#ff5c57,0.0); }
}
.mlabel { font-size:0.70em; font-weight:800; opacity:0.55; letter-spacing:.06em; }
.mvalue { font-size:1.7em; font-weight:800; }
.mvalue.hot { color:#ff5c57; }        /* value turns red when a sensor is near its crit limit */
.munit  { opacity:0.50; font-size:0.9em; }
.msub   { opacity:0.55; font-size:0.76em; }
.filter-group { font-size:0.72em; font-weight:800; opacity:0.5; letter-spacing:.05em; margin-top:6px; }
.specitem-label { opacity:0.55; font-size:0.74em; }
.specitem-val    { font-weight:700; }
.specitem-val.on  { color:@accent_color; }   /* active limit indicator */
.specitem-val.off { opacity:0.4; }           /* inactive */

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


def _card_bdf(card):
    return os.path.basename(os.path.realpath(os.path.join(card, "device"))) if card else None


def list_xe_gpus():
    """All Intel Arc (xe) GPUs on the box, sorted by PCI address. Each: card path, BDF,
    device id, and a short label."""
    out = []
    for c in sorted(glob.glob("/sys/class/drm/card*")):
        drv = os.path.join(c, "device", "driver")
        if os.path.islink(drv) and os.path.basename(os.path.realpath(drv)) == "xe":
            bdf = _card_bdf(c)
            did = (_read(os.path.join(c, "device", "device")) or "").replace("0x", "")
            out.append({"card": c, "bdf": bdf, "id": "8086:" + did,
                        "label": f"{os.path.basename(c)} · {bdf}"})
    return sorted(out, key=lambda g: g["bdf"] or "")


class XeGpu:
    def __init__(self, card=None):
        # a specific drm card, or the first xe card found
        self.card = card
        if self.card is None:
            for c in sorted(glob.glob("/sys/class/drm/card*")):
                drv = os.path.join(c, "device", "driver")
                if os.path.islink(drv) and os.path.basename(os.path.realpath(drv)) == "xe":
                    self.card = c
                    break
        self.bdf = _card_bdf(self.card)
        # pair the hwmon that belongs to THIS card (by BDF), not just the first xe hwmon
        self.hwmon = None
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if _read(os.path.join(d, "name")) != "xe":
                continue
            hbdf = os.path.basename(os.path.realpath(os.path.join(d, "device"))) \
                if os.path.exists(os.path.join(d, "device")) else None
            if self.bdf is None or hbdf == self.bdf:
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

    def vram(self):
        # VRAM used/total bytes, exported by xe-gpu-vram.service (from root-only debugfs).
        # Per-card file first (multi-GPU), then the default single-card file.
        raw = _read(f"/run/xe-gpu-vram-{self.bdf}") if self.bdf else None
        if not raw:
            raw = _read("/run/xe-gpu-vram")
        if not raw:
            return None
        p = raw.split()
        try:
            used, total = int(p[0]), int(p[1])
        except (IndexError, ValueError):
            return None
        return {"used": used, "total": total} if total > 0 else None

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

    def probe(self):
        # CHEAP registry probe for building the dashboard: sensor labels only (no live
        # *_input reads) + the static max clock + VRAM total. Avoids the slow first-wake
        # + per-channel temp reads that a full snapshot() does, so construction/GPU-switch
        # never blocks the UI thread. The real values arrive via the async _tick snapshot.
        rp0 = _int(os.path.join(self.card or "", "device/tile0/gt0/freq0/rp0_freq")) or 2900
        mains, vram = [], []
        for lbl, _f, _crit in self.tmap():
            (vram if lbl.startswith("vram_ch_") else mains).append({"label": lbl})
        return {"clocks": {"rp0": rp0}, "mains": mains, "vram": vram, "vmem": self.vram()}

    def snapshot(self):
        # one full read of everything — call this OFF the main thread
        return {"id": self.identity(), "clocks": self.clocks(), "power": self.power(),
                "fan": self.fan(), "mains": self.temps_where(False),
                "vram": self.temps_where(True), "energy": self.energy_uj(),
                "energy2": self.energy2_uj(), "throttle_flags": self.throttle_flags(),
                "profile": self.power_profile(), "vmem": self.vram()}

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


TEMP_RGB = (0.36, 0.62, 0.82)     # normal temperature line — neutral blue (no green)
HOT_RGB = (1.0, 0.36, 0.34)       # red, used for the line + number when a sensor runs hot


def temp_hot(c, crit):
    # "hot" = within 12 °C of the sensor's crit limit (absolute >=88 °C if crit unknown)
    return bool(c >= (crit - 12)) if crit else (c >= 88)


def temp_view(c, crit):
    """Neutral by default; red line + red number when the sensor is hot. Returns
    (line_rgb, value_state) where value_state is 'hot' or None."""
    hot = temp_hot(c, crit)
    return (HOT_RGB if hot else TEMP_RGB), ("hot" if hot else None)


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
                 group="GPU", core=False, section="metrics", icon=None):
        self.id = mid; self.label = label; self.unit = unit; self.compute = compute
        self.spark = spark; self.fixed = fixed; self.default = default; self.group = group
        self.core = core          # core metrics default to visible; all are filter-toggleable
        self.section = section    # "metrics" or "temps" — which dashboard section the tile lives in
        self.icon = icon          # custom vector icon key (freq/power/fan/temp/vram)


FLAME_MIN_C = 60          # only flag the hottest sensor once it's actually warm (not at idle)


def _temp_metric(lbl):
    def f(d):
        t = d["temp_by_label"].get(lbl)
        if not t:
            return {"text": "—"}
        rgb, st = temp_view(t["c"], t["crit"])
        flame = " 🔥" if (d.get("_hottest") == t["c"] and t["c"] >= FLAME_MIN_C) else ""
        return {"text": str(t["c"]) + flame, "val": t["c"], "state": st, "rgb": rgb,
                "sub": (f"limit {t['crit']}°C" if t.get("crit") else None)}
    return f


def _temp_pct(d):
    t = d["temp_by_label"].get("pkg")
    if not t or not t.get("crit"):
        return {"text": "—"}
    p = round(t["c"] / t["crit"] * 100)
    rgb, st = temp_view(t["c"], t["crit"])
    return {"text": str(p), "val": p, "state": st, "rgb": rgb}


def _limit(flag_keys):
    def f(d):
        on = any((d.get("throttle_flags") or {}).get(k) for k in flag_keys)
        return {"text": "yes", "state": "hot", "rgb": HOT_RGB} if on else {"text": "no"}
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
               spark=True, fixed=(0, rp0), core=True, group="Clocks", icon="freq"),
        Metric("power_card", "GPU Card Power", "W",
               lambda d: {"text": (f"{d['draw_card']:.0f}" if d.get("draw_card") is not None else "—"),
                          "val": d.get("draw_card")}, spark=True, core=True, group="Power", icon="power"),
        Metric("fan", "GPU Fan Speed", "rpm",
               lambda d: {"text": _num(d["fan"].get("rpm")), "val": d["fan"].get("rpm"),
                          "sub": (f"{round((d['fan'].get('duty') or 0) / 255 * 100)}% · "
                                  f"{d['fan'].get('mode', '?')}" if d["fan"].get("duty") is not None
                                  else d["fan"].get("mode"))},
               spark=True, fixed=(0, 5000), core=True, group="Fan", icon="fan"),
        # --- optional (filter, hidden by default) ---
        Metric("freq_act", "GPU Actual Frequency", "MHz",
               lambda d: {"text": _num(d["clocks"].get("act")), "val": d["clocks"].get("act")},
               spark=True, fixed=(0, rp0), default=False, group="Clocks", icon="freq"),
        Metric("power_gpu", "GPU Power", "W",
               lambda d: {"text": (f"{d['draw_pkg']:.0f}" if d.get("draw_pkg") is not None else "—"),
                          "val": d.get("draw_pkg")}, spark=True, default=False, group="Power", icon="power"),
        Metric("power_pct", "GPU Power Percent", "%",
               lambda d: ({"text": str(round(d["draw_card"] / d["power"]["cap_w"] * 100)),
                           "val": round(d["draw_card"] / d["power"]["cap_w"] * 100)}
                          if d.get("draw_card") is not None and d["power"].get("cap_w") else {"text": "—"}),
               spark=True, fixed=(0, 100), default=False, group="Power", icon="power"),
        Metric("fan_pct", "GPU Fan Duty", "%",
               lambda d: {"text": (str(round((d["fan"].get("duty") or 0) / 255 * 100))
                                   if d["fan"].get("duty") is not None else "—"),
                          "val": (round((d["fan"].get("duty") or 0) / 255 * 100)
                                  if d["fan"].get("duty") is not None else None),
                          "sub": (f"{d['fan'].get('rpm')} rpm" if d["fan"].get("rpm") is not None else None)},
               spark=True, fixed=(0, 100), default=False, group="Fan", icon="fan"),
        Metric("temp_pct", "GPU Temperature Percent", "%", _temp_pct,
               spark=True, fixed=(0, 100), default=False, group="Temperature", icon="temp"),
        Metric("vram_used", "VRAM Used", "GiB",
               lambda d: ({"text": f"{d['vmem']['used'] / 1073741824:.1f}",
                           "val": d["vmem"]["used"] / 1073741824}
                          if d.get("vmem") else {"text": "—", "sub": "needs xe-gpu-vram.service"}),
               spark=True, fixed=(0, ((sample.get("vmem") or {}).get("total") or 25_769_803_776) / 1073741824),
               default=False, group="VRAM", icon="vram"),
        Metric("vram_pct", "VRAM Usage", "%",
               lambda d: ({"text": str(round(d["vmem"]["used"] / d["vmem"]["total"] * 100)),
                           "val": round(d["vmem"]["used"] / d["vmem"]["total"] * 100)}
                          if d.get("vmem") else {"text": "—", "sub": "needs xe-gpu-vram.service"}),
               spark=True, fixed=(0, 100), default=False, group="VRAM", icon="vram"),
    ]


def build_temp_metrics(sample):
    """Per-sensor temperature tiles — one metric each, so they're filterable and never
    duplicated. pkg is core (always shown); the rest are optional."""
    out = []
    mains = [t["label"] for t in sample.get("mains", [])]
    chans = sorted([t["label"] for t in sample.get("vram", [])],
                   key=lambda x: int(x.rsplit("_", 1)[-1]))
    for lbl in mains + chans:
        label = ("VRAM ch " + lbl.rsplit("_", 1)[-1]) if lbl.startswith("vram_ch_") else _temp_label(lbl)
        out.append(Metric(f"temp_{lbl}", label, "°C", _temp_metric(lbl), spark=True,
                          fixed=(20, 110), default=False, core=(lbl == "pkg"),
                          group="Temperatures", section="temps", icon="temp"))
    return out


# ---- small custom vector icons (drawn in Cairo so they always render + tint) ----
def _ic_freq(cr, w, h, col, frac=0.7):   # gauge / speedometer (needle sweeps to frac)
    cx, cy, R = w / 2, h - 2, min(w, h) / 2
    cr.set_source_rgba(*col, 0.9); cr.set_line_width(1.5)
    cr.arc(cx, cy, R - 1, math.pi, 2 * math.pi); cr.stroke()
    ang = math.pi + max(0.0, min(1.0, frac)) * math.pi   # left -> up -> right
    cr.move_to(cx, cy); cr.line_to(cx + (R - 2) * math.cos(ang), cy + (R - 2) * math.sin(ang))
    cr.stroke()


def _ic_power(cr, w, h, col, alpha=0.95):   # lightning bolt (alpha flickers under load)
    cr.set_source_rgba(*col, alpha); cx = w / 2
    pts = [(cx + 1, 1), (cx - 3.5, h * 0.58), (cx - 0.5, h * 0.58),
           (cx - 1.5, h - 1), (cx + 4, h * 0.4), (cx + 1, h * 0.4)]
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.close_path(); cr.fill()


def _ic_fan(cr, w, h, col):         # fan: hub + 3 curved blades
    cx, cy = w / 2, h / 2; R = min(w, h) / 2 - 0.5
    cr.set_source_rgba(*col, 0.92)
    for k in range(3):
        cr.save(); cr.translate(cx, cy); cr.rotate(k * 2 * math.pi / 3)
        cr.move_to(0, 0)
        cr.curve_to(R * 0.15, -R * 0.55, R * 0.95, -R * 0.4, R, 0)
        cr.curve_to(R * 0.8, R * 0.25, R * 0.35, R * 0.2, 0, 0)
        cr.close_path(); cr.fill(); cr.restore()
    cr.arc(cx, cy, R * 0.22, 0, 6.29); cr.set_source_rgba(*col, 1.0); cr.fill()


def _ic_temp(cr, w, h, col):        # thermometer: tube + bulb
    cx = w / 2; cr.set_source_rgba(*col, 0.95); cr.set_line_width(1.6)
    cr.move_to(cx, 2); cr.line_to(cx, h - 5); cr.stroke()
    cr.arc(cx, h - 3.5, 2.6, 0, 6.29); cr.fill()
    cr.set_line_width(1.1)
    for ty in (4, 7, 10):
        cr.move_to(cx + 1.5, ty); cr.line_to(cx + 4, ty); cr.stroke()


def _ic_vram(cr, w, h, col):        # RAM chip: body + die line + pins
    cr.set_source_rgba(*col, 0.95); cr.set_line_width(1.3)
    x0, y0, x1, y1 = 2.5, 2.5, w - 2.5, h - 4
    cr.rectangle(x0, y0, x1 - x0, y1 - y0); cr.stroke()
    cr.move_to(x0 + 2, (y0 + y1) / 2); cr.line_to(x1 - 2, (y0 + y1) / 2); cr.stroke()
    for i in range(4):
        px = x0 + (i + 0.5) * (x1 - x0) / 4
        cr.move_to(px, y1); cr.line_to(px, y1 + 2.4); cr.stroke()


ICONS = {"freq": _ic_freq, "power": _ic_power, "fan": _ic_fan, "temp": _ic_temp, "vram": _ic_vram}


_NUMRE = re.compile(r"^-?\d+(?:\.\d+)?$")


class MetricTile(Gtk.Box):
    """A dashboard metric card: icon + label, big value + unit, a sub-line, and a live
    bottom visual (scrolling sparkline, or a radial ring for % metrics). Animated:
    icons move with the data (fan spins, gauge sweeps, bolt flickers), values tween to
    their new reading, and the tile flashes when it crosses into hot."""
    def __init__(self, label, unit="", spark=True, fixed=None, icon=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.add_css_class("mtile"); self.set_hexpand(True)
        self.hist = collections.deque(maxlen=SPARK_MAXPTS)
        self._fixed = fixed
        self._rgb = (0.21, 0.52, 0.89)
        self._icon = icon
        self._ring = (unit == "%")              # percent metrics render as a radial ring
        # animation state
        self._spin = 0.0; self._needle = 0.7; self._anim = 0.0
        self._ring_cur = 0.0; self._ring_tgt = 0.0
        self._shown = None; self._dec = 0; self._tw_from = 0.0; self._tw_to = 0.0; self._tw_t = None
        self._was_hot = False; self._tick_id = None; self._last_t = 0
        head = Gtk.Box(spacing=5)
        if icon in ICONS:
            self.icon_area = Gtk.DrawingArea()
            self.icon_area.set_content_width(18); self.icon_area.set_content_height(15)
            self.icon_area.set_valign(Gtk.Align.CENTER)
            self.icon_area.set_draw_func(self._draw_icon)
            head.append(self.icon_area)
        else:
            self.icon_area = None
        self.lbl = Gtk.Label(label=label.upper(), xalign=0, hexpand=True); self.lbl.add_css_class("mlabel")
        head.append(self.lbl)
        if self._ring:      # small gauge ring tucked into the top-right corner (% metrics)
            self.ring_area = Gtk.DrawingArea()
            self.ring_area.set_content_width(26); self.ring_area.set_content_height(26)
            self.ring_area.set_valign(Gtk.Align.START)
            self.ring_area.set_draw_func(self._draw_ring)
            head.append(self.ring_area)
        else:
            self.ring_area = None
        self.append(head)
        vr = Gtk.Box(spacing=4)
        self.val = Gtk.Label(label="—", xalign=0); self.val.add_css_class("mvalue")
        vr.append(self.val)
        if unit:
            u = Gtk.Label(label=unit, xalign=0, valign=Gtk.Align.END); u.add_css_class("munit")
            vr.append(u)
        self.append(vr)
        self.sub = Gtk.Label(xalign=0); self.sub.add_css_class("msub"); self.sub.set_visible(False)
        self.append(self.sub)
        if spark:           # sparkline (line graph) at the bottom for every metric
            self.area = Gtk.DrawingArea(hexpand=True); self.area.set_content_height(30)
            self.area.set_draw_func(self._draw)
            self.append(self.area)
        else:
            self.area = None

    # ---- icon (animated) ----
    def _draw_icon(self, area, cr, w, h, *_):
        ic = self._icon
        if ic == "fan":
            cr.translate(w / 2, h / 2); cr.rotate(self._spin); cr.translate(-w / 2, -h / 2)
            _ic_fan(cr, w, h, self._rgb)
        elif ic == "freq":
            _ic_freq(cr, w, h, self._rgb, self._needle)
        elif ic == "power":
            fl = 0.95 - (0.32 * abs(math.sin(self._last_t / 1.4e5))) if self._anim > 0.6 else 0.95
            _ic_power(cr, w, h, self._rgb, fl)
        elif ic in ICONS:
            ICONS[ic](cr, w, h, self._rgb)

    # ---- update ----
    def update(self, text, spark_val=None, rgb=None, state=None, sub=None):
        if rgb is not None and rgb != self._rgb:
            self._rgb = rgb
            if self.icon_area is not None:
                self.icon_area.queue_draw()
        hot = (state == "hot")
        if hot and not self._was_hot:           # flash on crossing into hot
            self.add_css_class("flash")
            GLib.timeout_add(700, lambda: (self.remove_css_class("flash"), False)[1])
        self._was_hot = hot
        (self.val.add_css_class if hot else self.val.remove_css_class)("hot")
        # value: tween when it's a plain number, else set directly
        if _NUMRE.match(text):
            tgt = float(text); self._dec = len(text.split(".")[1]) if "." in text else 0
            if self._shown is None:
                self._shown = tgt; self.val.set_text(text)
            elif abs(tgt - self._shown) > 1e-9:
                self._tw_from = self._shown; self._tw_to = tgt; self._tw_t = 0.0
        else:
            self._shown = None; self.val.set_text(text)
        if sub is not None:
            self.sub.set_text(sub); self.sub.set_visible(bool(sub))
        # animation targets
        if spark_val is not None:
            if self._fixed:
                lo, hi = self._fixed
                self._anim = max(0.0, min(1.0, (spark_val - lo) / (hi - lo))) if hi > lo else 0.0
            if self._ring:
                self._ring_tgt = max(0.0, min(100.0, spark_val))
            if self.area is not None:               # sparkline for every metric
                self.hist.append(spark_val); self.area.queue_draw()
        self._ensure_tick()

    def _ensure_tick(self):
        if self._tick_id is None:
            self._last_t = 0
            self._tick_id = self.add_tick_callback(self._tick)

    def _tick(self, widget, clock):
        now = clock.get_frame_time()
        dt = (now - self._last_t) / 1e6 if self._last_t else 0.016
        self._last_t = now
        busy = False
        if self._tw_t is not None:              # number tween (ease-out)
            self._tw_t += dt / 0.22
            if self._tw_t >= 1.0:
                self._shown = self._tw_to; self._tw_t = None
                self.val.set_text(self._fmt(self._shown))
            else:
                e = 1 - (1 - self._tw_t) ** 3
                self.val.set_text(self._fmt(self._tw_from + (self._tw_to - self._tw_from) * e))
                busy = True
        if self._icon == "fan" and self.icon_area and self._anim > 0.01:
            self._spin = (self._spin + dt * (0.6 + self._anim * 8.0)) % (2 * math.pi)
            self.icon_area.queue_draw(); busy = True
        elif self._icon == "freq" and self.icon_area and abs(self._needle - self._anim) > 0.003:
            self._needle += (self._anim - self._needle) * min(dt * 6.0, 1.0)
            self.icon_area.queue_draw(); busy = True
        elif self._icon == "power" and self.icon_area and self._anim > 0.6:
            self.icon_area.queue_draw(); busy = True
        if self._ring and self.ring_area and abs(self._ring_cur - self._ring_tgt) > 0.2:
            self._ring_cur += (self._ring_tgt - self._ring_cur) * min(dt * 6.0, 1.0)
            self.ring_area.queue_draw(); busy = True
        if not busy:
            self._tick_id = None
            return False
        return True

    def _fmt(self, v):
        return f"{v:.{self._dec}f}"

    # ---- bottom sparkline ----
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
        # glowing leading dot
        lx, ly = X(n - 1), Y(self.hist[-1])
        cr.set_source_rgba(r, g, b, 0.30); cr.arc(lx, ly, 3.2, 0, 6.29); cr.fill()
        cr.set_source_rgba(r, g, b, 1.0); cr.arc(lx, ly, 1.6, 0, 6.29); cr.fill()

    def _draw_ring(self, area, cr, w, h, *_):
        cx, cy, R = w / 2, h / 2, min(w, h) / 2 - 2
        r, g, b = self._rgb
        a0 = math.radians(135); a1 = math.radians(135 + 270)
        cr.set_line_width(3.0); cr.set_line_cap(1)     # round caps
        cr.set_source_rgba(r, g, b, 0.16); cr.arc(cx, cy, R, a0, a1); cr.stroke()
        frac = max(0.0, min(1.0, self._ring_cur / 100.0))
        cr.set_source_rgba(r, g, b, 0.95); cr.arc(cx, cy, R, a0, a0 + (a1 - a0) * frac); cr.stroke()


HELPER_PATHS = {"xe-fan-curve": "/usr/local/bin/xe-fan-curve",
                "xe-gpu-tune": "/usr/local/bin/xe-gpu-tune",
                "xe-gpu-oc": "/usr/local/bin/xe-gpu-oc",
                "xe-gpu-stress": "/usr/local/bin/xe-gpu-stress"}


def run_priv(args, parent, after=None):
    # pkexec sanitizes PATH (often no /usr/local/bin) -> use absolute paths.
    args = [HELPER_PATHS.get(args[0], args[0])] + list(args[1:])
    cmd = ["pkexec"]
    # multi-GPU: pkexec strips the env, so pass the selected card's BDF via `env`
    bdf = getattr(getattr(parent, "gpu", None), "bdf", None)
    if getattr(parent, "_multi_gpu", False) and bdf:
        cmd += ["/usr/bin/env", f"ARC_GPU_BDF={bdf}"]
    cmd += args
    try:
        p = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
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
        self.vsec = self.msec = self.tsec = None   # OC section boxes (gated on unsupported GPUs)
        self._oc_gated_done = False

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
        self.prof_save_btn = icon_button("list-add-symbolic", "Save the current settings as a new profile",
                                         self._prof_save, label="Save")
        top.append(self.prof_save_btn)
        self.prof_load_btn = icon_button("document-open-symbolic", "Load the selected profile",
                                         self._prof_load, label="Load")
        top.append(self.prof_load_btn)
        self.prof_del_btn = icon_button("user-trash-symbolic", "Delete the selected profile", self._prof_delete)
        top.append(self.prof_del_btn)
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
        self.vsec = vsec
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
            msec.append(mgrid); self.msec = msec; right.append(msec)

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
            tsec.append(tgrid); self.tsec = tsec; right.append(tsec)

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
        self.reset_btn = icon_button("edit-undo-symbolic", "Restore stock curve + memory + temp",
                                     self._reset, label="Reset")
        bar.append(self.reset_btn)
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
        self._apply_oc_gate(bool(data))
        return False

    def _apply_oc_gate(self, functional):
        # The voltage-curve / memory-speed / temperature-limit controls ride on the
        # xe_gt_oc PCODE ops, which the Arc Pro B70 (Battlemage G31) firmware rejects
        # (the driver reports them unsupported, so vf_curve reads back empty). Gray those
        # sections out — power cap and clock limits (plain driver sysfs) keep working.
        if functional or self._oc_gated_done:
            return
        self._oc_gated_done = True
        for w in (self.vsec, self.msec, self.tsec, self.prof_dd, self.prof_save_btn,
                  self.prof_load_btn, self.prof_del_btn, self.reset_btn):
            if w is not None:
                w.set_sensitive(False)
        banner = Adw.Banner(title=(
            "Overclocking isn't available on this GPU — its firmware doesn't expose the "
            "voltage curve, memory speed, or temperature limit. Power and clock limits still work."))
        banner.set_revealed(True)
        self.prepend(banner)

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
    def _loader_cands(self):
        # GPU load generators appropriate for the session. On Wayland the plain
        # X11/GLX `glmark2` can't open a display, so it does NOT count — we need
        # a Wayland-native binary (glmark2-wayland / vkmark).
        if os.environ.get("WAYLAND_DISPLAY"):
            return ("glmark2-wayland", "vkmark", "glmark2-es2-wayland", "glxgears")
        return ("glmark2", "vkmark", "glxgears")

    def _stress(self, *_):
        if self._testing:
            return
        # need a session-appropriate load generator; if none, offer to install
        # one and then run automatically (see _install_loader).
        if not any(shutil.which(x) for x in self._loader_cands()):
            self._prompt_install_loader()
            return
        self._run_stress()

    def _prompt_install_loader(self):
        dlg = Adw.MessageDialog(
            transient_for=self.window, heading="Install stability-test tool?",
            body="The stability test drives the GPU with a load generator, which "
                 "isn’t installed for this session yet. Install it now and run "
                 "the test?")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("install", "Install & Test")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("install"); dlg.set_close_response("cancel")
        dlg.connect("response",
                    lambda d, r: self._install_loader() if r == "install" else None)
        dlg.present()

    def _install_loader(self):
        # On Wayland (+apt) install the Wayland-native glmark2 build; the plain
        # `glmark2` package is X11-only and won't run on a Wayland session.
        apt = shutil.which("apt-get")
        pkg = ("glmark2-wayland" if os.environ.get("WAYLAND_DISPLAY") else "glmark2") if apt else "glmark2"
        if   apt:                     cmd = ["pkexec", "apt-get", "install", "-y", pkg]
        elif shutil.which("dnf"):     cmd = ["pkexec", "dnf", "install", "-y", pkg]
        elif shutil.which("pacman"):  cmd = ["pkexec", "pacman", "-S", "--noconfirm", pkg]
        elif shutil.which("zypper"):  cmd = ["pkexec", "zypper", "--non-interactive", "install", pkg]
        else:
            self.window.toast("No supported package manager — install glmark2 manually", ms=4000)
            return
        self._status_open(f"Installing {pkg}…")

        def work():
            try:
                rc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode
            except OSError:
                rc = -1
            GLib.idle_add(self._install_done, rc)
        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, rc):
        if rc == 0 and any(shutil.which(x) for x in self._loader_cands()):
            self._status_set("Starting stability test…")   # modal stays; _stress_* drive it
            self._run_stress()
        else:
            self._status_close()
            self.window.toast("Install cancelled" if rc == 126 else
                              "Could not install the test tool", ms=4000)
        return False

    # -- status modal (spinner + updatable label); shared by install + test --
    def _status_open(self, text):
        self._status_close()
        w = Adw.Window(transient_for=self.window, modal=True, resizable=False,
                       default_width=340, title="")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      margin_top=28, margin_bottom=28, margin_start=28, margin_end=28)
        sp = Gtk.Spinner(width_request=32, height_request=32); sp.start()
        lbl = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
        box.append(sp); box.append(lbl)
        w.set_content(box); w.present()
        self._status_win, self._status_lbl = w, lbl

    def _status_set(self, text):
        if getattr(self, "_status_lbl", None):
            self._status_lbl.set_text(text)

    def _status_close(self):
        w = getattr(self, "_status_win", None)
        if w:
            w.close()
        self._status_win = self._status_lbl = None

    def _run_stress(self):
        self._testing = True
        self.test_btn.set_sensitive(False); self.apply_btn.set_sensitive(False)
        self.hint.set_text("stability test starting…")
        self.window.toast(f"Stability test: current settings, {STRESS_SECS}s load (fan → max)…")
        # run via pkexec with --fan-guard: root ramps the fan to max + restores it, and
        # runs the workload as us (passing our session env so it can open the display).
        env = os.environ
        cmd = ["pkexec", HELPER_PATHS["xe-gpu-stress"], str(STRESS_SECS), "--fan-guard",
               "--user", env.get("USER") or env.get("LOGNAME") or "",
               "--display", env.get("DISPLAY", ""),
               "--wayland", env.get("WAYLAND_DISPLAY", ""),
               "--runtime", env.get("XDG_RUNTIME_DIR", "")]
        # multi-GPU: pin the GL workload to the SELECTED card (else it loads the primary)
        bdf = getattr(self.window.gpu, "bdf", None)
        if getattr(self.window, "_multi_gpu", False) and bdf:
            cmd += ["--dri", "pci-" + bdf.replace(":", "_").replace(".", "_")]

        # test the CURRENT (possibly unapplied) UI settings: xe-gpu-stress snapshots
        # the live OC, applies these directly to sysfs — NOT persisted — for the test,
        # and restores them after, so a hang can't leave an unstable OC saved for boot.
        if getattr(self, "stock", None):
            tgt = self._target_curve()
            cmd += ["--oc-curve", " ".join(f"{i}:{mv}" for i, mv in enumerate(tgt))]
            if self.f_mem is not None:
                cmd += ["--oc-mem", str(int(self.f_mem.value * 1000))]
            if self.f_temp is not None:
                cmd += ["--oc-temp", str(int(self.f_temp.value))]

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
        self._status_set(f"Testing… {sec}s / {STRESS_SECS}s\n{mhz} MHz · {tc}°C")
        return False

    def _stress_done(self, s):
        self._testing = False
        self._status_close()
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
            self._result_dialog(
                "Unstable under load ✗",
                f"A GPU hang or crash was detected under sustained load — this "
                f"overclock is NOT stable.\n\n"
                f"Duration:  {STRESS_SECS}s at full load\n"
                f"Peak temp:  {mt}°C\n\n"
                f"Reverting to stock settings.")
            run_priv(["xe-gpu-oc", "reset"], self.window,
                     lambda: (setattr(self, "applied", 0),
                              setattr(self, "applied_curve", None), self._load()))
        elif st == "throttled":
            self._result_dialog(
                "Stable, but thermally throttled ⚠",
                f"Ran the full load with no hang, but the GPU hit its temperature "
                f"limit and lowered clocks to stay safe.\n\n"
                f"Duration:  {STRESS_SECS}s at full load\n"
                f"Peak temp:  {mt}°C  (limit {s.get('TEMPLIMIT', '?')}°C)\n"
                f"Clocks:  {mnf}–{mxf} MHz")
        else:
            self._result_dialog(
                "Stability test passed ✓",
                f"Ran the full load with no hang or crash — this overclock looks "
                f"stable.\n\n"
                f"Duration:  {STRESS_SECS}s at full load\n"
                f"Peak temp:  {mt}°C\n"
                f"Clocks:  {mnf}–{mxf} MHz")

    def _result_dialog(self, heading, body):
        dlg = Adw.MessageDialog(transient_for=self.window, heading=heading, body=body)
        dlg.add_response("ok", "Close")
        dlg.set_default_response("ok"); dlg.set_close_response("ok")
        dlg.present()
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

        prov = Gtk.CssProvider(); prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.gpus = list_xe_gpus()
        self._multi_gpu = len(self.gpus) > 1
        self._tick_id = None
        self._reading = False

        self._tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self._switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        hb.set_title_widget(self._switcher)
        # GPU selector — only when there's more than one xe card
        if self._multi_gpu:
            gi = Gtk.Image(icon_name="video-display-symbolic")
            hb.pack_start(gi)
            self.gpu_dd = Gtk.DropDown.new_from_strings([g["label"] for g in self.gpus])
            self.gpu_dd.set_tooltip_text("Choose which GPU to monitor and control")
            self.gpu_dd.connect("notify::selected", self._on_gpu_change)
            hb.pack_start(self.gpu_dd)
        else:
            self.gpu_dd = None
        hb.pack_start(icon_button("view-refresh-symbolic", "Refresh readings now",
                                  lambda *_: self.refresh()))
        self._tv.add_top_bar(hb)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self._tv)
        self.set_content(self.toasts)

        if not self.gpus:
            empty = Adw.ViewStack()
            empty.add_titled(Gtk.Label(label="No Intel xe GPU found", margin_top=40), "none", "No GPU")
            self._switcher.set_stack(empty); self._tv.set_content(empty)
            self.stack = empty
            return

        self.gpu = XeGpu(self.gpus[0]["card"])
        self._build_content()

    def _build_content(self):
        # (re)build all tabs for the currently-selected GPU. Called on GPU switch.
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id); self._tick_id = None
        self.stack = Adw.ViewStack()
        self._switcher.set_stack(self.stack)
        self._tv.set_content(self.stack)

        p1 = self.stack.add_titled(self._build_dashboard(), "dash", "Dashboard")
        p1.set_icon_name("utilities-system-monitor-symbolic")

        self.editor = CurveEditor(self.gpu, self)
        curve_wrap = Gtk.Box(margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        curve_wrap.append(self.editor)
        p2 = self.stack.add_titled(curve_wrap, "curve", "Fan Control")
        p2.set_icon_name("power-profile-balanced-symbolic")

        self.oc_view = None
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
        self._tick_id = GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def _on_gpu_change(self, dd, _):
        i = dd.get_selected()
        if 0 <= i < len(self.gpus):
            self.gpu = XeGpu(self.gpus[i]["card"])
            self._build_content()
            self.toast(f"Now controlling {self.gpus[i]['label']} ({self.gpus[i]['id']})")

    def toast(self, msg, ms=2500):
        # timeout=0 keeps Adw from auto-dismissing; we dismiss manually for a precise 2.5s.
        t = Adw.Toast(title=msg, timeout=0)
        self.toasts.add_toast(t)
        GLib.timeout_add(ms, lambda: (t.dismiss(), False)[1])

    def _build_dashboard(self):
        sample = self.gpu.probe()          # cheap: labels + rp0 + vram total, no UI-thread stall
        self.metrics = build_metrics(sample) + build_temp_metrics(sample)
        self.metric_by_id = {m.id: m for m in self.metrics}
        saved = load_config().get("metrics", {})
        # every metric is toggleable; the four "core" ones just default to visible
        self.visible = {m.id: bool(saved.get(m.id, m.default or m.core)) for m in self.metrics}
        self.tiles = {}
        self._energy_prev = {}
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self._build_specs(page)                    # Specifications — full-width top row
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        mh = Gtk.Box(spacing=6)
        ml = Gtk.Label(label="METRICS", xalign=0, hexpand=True); ml.add_css_class("section")
        mh.append(ml); mh.append(self._build_filter_button())
        body.append(mh)
        self.metrics_flow = self._tile_flow([m for m in self.metrics if m.section == "metrics"])
        body.append(self.metrics_flow)
        th = Gtk.Label(label="TEMPERATURES", xalign=0); th.add_css_class("section"); th.set_margin_top(8)
        body.append(th)
        self.temps_flow = self._tile_flow([m for m in self.metrics if m.section == "temps"])
        body.append(self.temps_flow)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_child(body)
        page.append(sw)
        self._build_controls(page)                 # power/clock card only when there's no OC tab
        return page

    def _tile_flow(self, metrics):
        flow = Gtk.FlowBox(column_spacing=10, row_spacing=10, homogeneous=True,
                           min_children_per_line=2, max_children_per_line=6,
                           selection_mode=Gtk.SelectionMode.NONE)
        flow.set_filter_func(self._metric_visible)
        for m in metrics:
            tile = MetricTile(m.label, m.unit, spark=m.spark, fixed=m.fixed, icon=m.icon)
            tile.metric_id = m.id
            self.tiles[m.id] = tile
            flow.append(tile)
        return flow

    def _metric_visible(self, child):
        mid = getattr(child.get_child(), "metric_id", None)
        return bool(self.visible.get(mid, False))

    def _refilter(self):
        self.metrics_flow.invalidate_filter(); self.temps_flow.invalidate_filter()

    def _build_filter_button(self):
        btn = Gtk.Button(tooltip_text="Choose which metrics & temperatures to show "
                                      "(saved across launches)")
        btn.set_child(Adw.ButtonContent(icon_name="view-list-symbolic", label="Metrics"))
        btn.connect("clicked", lambda *_: self._open_filter_dialog())
        return btn

    def _open_filter_dialog(self):
        dlg = Adw.Window(transient_for=self, modal=True, title="Choose metrics",
                         default_width=580, default_height=600)
        tv = Adw.ToolbarView(); hb = Adw.HeaderBar()
        ab = Gtk.Button(label="All"); ab.connect("clicked", lambda *_: self._filter_bulk(True))
        nb = Gtk.Button(label="None"); nb.connect("clicked", lambda *_: self._filter_bulk(False))
        hb.pack_start(ab); hb.pack_start(nb)
        done = Gtk.Button(label="Done"); done.add_css_class("suggested-action")
        done.connect("clicked", lambda *_: dlg.close()); hb.pack_end(done)
        tv.add_top_bar(hb)
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24,
                       margin_start=16, margin_end=16, margin_top=12, margin_bottom=16)
        colA = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        colB = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        cols.append(colA); cols.append(colB)
        self._filter_checks = {}

        order = {"Clocks": 0, "Power": 1, "Fan": 2, "Temperature": 3, "VRAM": 4, "Temperatures": 5}

        def fill(parent, items):
            lastgroup = None
            for m in sorted(items, key=lambda x: order.get(x.group, 99)):   # contiguous groups
                if m.group != lastgroup:
                    gl = Gtk.Label(label=m.group.upper(), xalign=0); gl.add_css_class("filter-group")
                    parent.append(gl); lastgroup = m.group
                cb = Gtk.CheckButton(label=m.label)
                cb.set_active(self.visible.get(m.id, False))
                cb.connect("toggled", self._on_filter_toggle, m.id)
                self._filter_checks[m.id] = cb
                parent.append(cb)
        fill(colA, [m for m in self.metrics if m.section == "metrics"])   # metric groups
        fill(colB, [m for m in self.metrics if m.section == "temps"])     # temperature sensors
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_child(cols)
        tv.set_content(sw); dlg.set_content(tv)
        dlg.present()

    def _on_filter_toggle(self, cb, mid):
        self.visible[mid] = cb.get_active()
        self._persist_filter(); self._refilter()

    def _filter_bulk(self, val):
        for m in self.metrics:
            self.visible[m.id] = val
            if m.id in self._filter_checks:
                self._filter_checks[m.id].set_active(val)
        self._persist_filter(); self._refilter()

    def _persist_filter(self):
        cfg = load_config(); cfg["metrics"] = self.visible; save_config(cfg)

    def _build_specs(self, parent):
        c = card("Specifications", "Fixed limits & configuration — not live metrics.")
        self.spec_rows = {}
        flow = Gtk.FlowBox(column_spacing=24, row_spacing=8, homogeneous=False,
                           min_children_per_line=2, max_children_per_line=8,
                           selection_mode=Gtk.SelectionMode.NONE)
        rows = [("device", "Device"), ("vram", "VRAM"), ("cap", "Power cap"),
                ("limit", "Power limit (I1)"), ("clk", "Clock limits"), ("hw", "Hardware range"),
                ("profile", "Power profile"), ("fan", "Fan mode"), ("power_lim", "Power limited"),
                ("temp_lim", "Temp limited"), ("volt_lim", "Voltage limited")]
        for key, label in rows:
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            k = Gtk.Label(label=label, xalign=0); k.add_css_class("specitem-label")
            v = Gtk.Label(label="—", xalign=0); v.add_css_class("specitem-val")
            item.append(k); item.append(v)
            flow.append(item); self.spec_rows[key] = v
        c.append(flow); parent.append(c)

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

    def refresh(self, *_):
        self._tick()      # trigger an async read (used by control after-callbacks)
        return False

    def _tick(self):
        # ALL sysfs reads run off the main thread — the first read after GPU idle
        # forces a wake (~0.8-1.4s); doing it here would freeze the UI.
        if not getattr(self, "gpu", None) or self._reading:
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
        vr = data.get("vmem")
        self.spec_rows["vram"].set_text(f"{vr['total'] / 1073741824:.1f} GiB" if vr else "—")
        self.spec_rows["cap"].set_text(f"{pw['cap_w']} W" if pw.get("cap_w") else "—")
        self.spec_rows["limit"].set_text(f"{pw['crit_w']:.0f} W" if pw.get("crit_w") else "—")
        self.spec_rows["clk"].set_text(f"{cl.get('min','—')}–{cl.get('max','—')} MHz")
        self.spec_rows["hw"].set_text(f"{cl.get('rpn','—')}–{cl.get('rp0','—')} MHz")
        self.spec_rows["profile"].set_text(prof.get("current", "—"))
        self.spec_rows["fan"].set_text(data["fan"].get("mode", "—"))
        tf = data.get("throttle_flags") or {}

        def _chk(row, on):
            w = self.spec_rows[row]
            w.set_text("✓" if on else "✗")
            w.remove_css_class("off" if on else "on")
            w.add_css_class("on" if on else "off")
        _chk("power_lim", any(tf.get(k) for k in ("pl1", "pl2", "pl4")))
        _chk("temp_lim", tf.get("thermal"))
        _chk("volt_lim", tf.get("vr_tdc"))
        # --- Metrics + Temperatures (one unified pass; temps are metrics too) ---
        data["_hottest"] = max((t["c"] for t in data["mains"] + data["vram"]), default=None)
        for m in self.metrics:
            tile = self.tiles.get(m.id)
            if tile is None or not self.visible.get(m.id):
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
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.exzile.XeGpuDashboard",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    App().run(None)
