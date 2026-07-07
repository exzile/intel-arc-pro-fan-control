// arc-gpu-gui — native Win32 dashboard + fan-curve editor for Intel Arc (IGCL).
//
// A self-contained GDI app over arc_core — no external UI toolkit, builds with
// the same MSVC toolchain as the CLI/service. Windows analogue of the Linux GTK4
// `xe-gpu-gui`, with a GPU selector and three views:
//
//   Dashboard   live (1 s) clocks / power / temp / utilisation / fan / VRAM.
//   Fan         a draggable temperature->percent curve editor (drag nodes,
//               double-click to add, right-click to remove, live temp marker),
//               plus Fan Auto / Fan Max / Apply / Reset.
//   Overclock   frequency / voltage offsets + power / temp / mem limits with
//               Apply and Reset-to-stock.
//
// All fan/OC writes go through the SYSTEM service (the GUI edits the selected
// adapter's profile and nudges the service), so the non-elevated GUI never drives
// IGCL directly. Per-adapter: edits target the card chosen in the dropdown.
#include <windows.h>
#include <windowsx.h>
#include <commctrl.h>
#include <shellapi.h>
#include <cstdio>
#include <cmath>
#include <string>
#include <vector>
#include <algorithm>

#include "../arc.hpp"
#include "../config.hpp"
#include "../fan_curve.hpp"
#include "../apply.hpp"

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "shell32.lib")

using namespace arc;

namespace {

#define WM_TRAYICON (WM_APP + 1)

enum : int {
    kIdCombo = 1001,
    kIdBtnDash = 1002,
    kIdBtnCurve = 1003,
    kIdBtnAuto = 1004,
    kIdBtnMax = 1005,
    kIdBtnApplyCurve = 1006,
    kIdBtnResetCurve = 1007,
    kIdBtnOc = 1008,
    kIdBtnApplyOc = 1009,
    kIdBtnResetOc = 1010,
    kIdCbPreset = 1011,
    kIdChkCurve = 1012,
    // Overclock sliders (trackbars). Order matches g_oc[].
    kIdTbFreq = 1020,
    kIdTbVolt = 1021,
    kIdTbVlim = 1022,
    kIdTbPower = 1023,
    kIdTbTemp = 1024,
    kIdTbMem = 1025,
    kIdTrayOpen = 1101,
    kIdTrayFanAuto = 1102,
    kIdTrayFanMax = 1103,
    kIdTrayExit = 1104,
    kTimerId = 1,
    kTrayIconId = 1,
};

// Live in the notification area; closing/minimising hides to tray, and the tray
// icon (single-click = open, right-click = menu) brings the window back.
NOTIFYICONDATAW g_nid{};
bool g_reallyExit = false;

void addTrayIcon(HWND hwnd) {
    g_nid = {};
    g_nid.cbSize = sizeof(g_nid);
    g_nid.hWnd = hwnd;
    g_nid.uID = kTrayIconId;
    g_nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    g_nid.uCallbackMessage = WM_TRAYICON;
    g_nid.hIcon = ::LoadIconW(nullptr, IDI_APPLICATION);
    wcscpy_s(g_nid.szTip, ARRAYSIZE(g_nid.szTip), L"Arc GPU Control");
    ::Shell_NotifyIconW(NIM_ADD, &g_nid);
}

void removeTrayIcon() { ::Shell_NotifyIconW(NIM_DELETE, &g_nid); }

void showMainWindow(HWND hwnd) {
    ::ShowWindow(hwnd, SW_SHOW);
    ::ShowWindow(hwnd, SW_RESTORE);
    ::SetForegroundWindow(hwnd);
}

void showTrayMenu(HWND hwnd) {
    HMENU m = ::CreatePopupMenu();
    ::AppendMenuW(m, MF_STRING, kIdTrayOpen, L"Open Arc GPU Control");
    ::AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
    ::AppendMenuW(m, MF_STRING, kIdTrayFanAuto, L"Fan: Auto (stock)");
    ::AppendMenuW(m, MF_STRING, kIdTrayFanMax, L"Fan: Max");
    ::AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
    ::AppendMenuW(m, MF_STRING, kIdTrayExit, L"Exit");
    POINT p; ::GetCursorPos(&p);
    ::SetForegroundWindow(hwnd);   // so the menu dismisses on click-away
    ::TrackPopupMenu(m, TPM_RIGHTBUTTON, p.x, p.y, 0, hwnd, nullptr);
    ::DestroyMenu(m);
}

enum class View { Dashboard, Fan, Overclock };

ArcController g_arc;
bool          g_ready = false;
std::string   g_initError;
View          g_view = View::Dashboard;

Telemetry  g_prev;
Metrics    g_metrics;
int        g_fanPct = -1;
MemoryInfo g_mem;
std::string g_deviceLine;

std::vector<FanPoint> g_curve;   // editor working copy
int g_dragIdx = -1;

HFONT g_fontLabel = nullptr;
HFONT g_fontValue = nullptr;
HWND  g_combo = nullptr;
HWND  g_btnDash = nullptr, g_btnCurve = nullptr, g_btnOc = nullptr;
HWND  g_btnAuto = nullptr, g_btnMax = nullptr;
HWND  g_btnApplyCurve = nullptr, g_btnResetCurve = nullptr;
HWND  g_btnApplyOc = nullptr, g_btnResetOc = nullptr, g_cbPreset = nullptr;

// One overclock knob = a labelled slider (trackbar) + value readout. Knobs marked
// b70Gated ride the xe_gt_oc PCODE ops the B70 firmware rejects, so they're greyed
// out (and a banner shown) when the B70 is selected — power/freq stay live.
struct OcSlider {
    int id; const wchar_t* label; int lo, hi; const wchar_t* unit; bool b70Gated;
    HWND tb = nullptr; HWND val = nullptr;
};
OcSlider g_oc[] = {
    { kIdTbFreq,  L"GPU Frequency Offset", -200, 200,  L"MHz",  false },
    { kIdTbVolt,  L"GPU Voltage Offset",   -150, 150,  L"mV",   true  },
    { kIdTbVlim,  L"Voltage Limit",         800, 1200, L"mV",   true  },
    { kIdTbPower, L"Power Limit",            50, 400,  L"W",    false },
    { kIdTbTemp,  L"Temperature Limit",      60, 110,  L"deg C",true  },
    { kIdTbMem,   L"VRAM Memory Speed",      15,  25,  L"GT/s", true  },
};
constexpr int kOcCount = sizeof(g_oc) / sizeof(g_oc[0]);
enum { OC_FREQ, OC_VOLT, OC_VLIM, OC_POWER, OC_TEMP, OC_MEM };
std::vector<VFPoint> g_vfStock;   // stock voltage-frequency curve for the OC graph
bool g_ocGated = false;           // current adapter is a gated (B70) card

// Manual VF-curve editing: a checkbox toggles offset-mode (uniform voltage shift
// via the slider) vs curve-mode (drag anchor nodes to shape voltage per freq).
HWND g_chkCurve = nullptr;
bool g_ocCurveMode = false;
std::vector<VFPoint> g_vfEdit;     // full editable curve (curve mode)
std::vector<int> g_vfAnchors;      // draggable anchor indices into g_vfEdit
int g_vfDrag = -1;                 // anchor being dragged (index into g_vfAnchors)

void ocUpdateVal(const OcSlider& s) {
    int v = (int)::SendMessageW(s.tb, TBM_GETPOS, 0, 0);
    wchar_t b[64]; ::wsprintfW(b, L"%d %s", v, s.unit);
    ::SetWindowTextW(s.val, b);
}
void ocSetPos(OcSlider& s, int v) {
    v = (v < s.lo) ? s.lo : (v > s.hi ? s.hi : v);
    ::SendMessageW(s.tb, TBM_SETPOS, TRUE, (LPARAM)v);
    ocUpdateVal(s);
}
int ocGetPos(const OcSlider& s) { return (int)::SendMessageW(s.tb, TBM_GETPOS, 0, 0); }

std::wstring widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring w(n ? n - 1 : 0, L'\0');
    if (n) ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
    return w;
}

std::string fmt(const char* f, double v) {
    char b[64]; std::snprintf(b, sizeof(b), f, v); return b;
}

void refreshDeviceLine() {
    if (const AdapterInfo* d = g_arc.current())
        g_deviceLine = d->name + "  (" + d->bdfString() + ")";
    else
        g_deviceLine = "(no adapter)";
}

std::vector<FanPoint> defaultCurve() {
    return {{30, 20}, {50, 40}, {65, 60}, {75, 80}, {85, 100}};
}

void loadEditorCurve() {
    std::string err;
    // Prefer the SELECTED adapter's saved curve (so switching cards shows that
    // card's profile); else the live curve; else a sane default.
    const AdapterInfo* d = g_arc.current();
    AppConfig cfg;
    loadConfigFor(d ? d->key() : std::string(), cfg, err);
    if (cfg.fanMode == FanMode::Curve && cfg.curve.size() >= 2) {
        g_curve = cfg.curve;
    } else if (!g_arc.fanGetCurve(g_curve, err) || g_curve.size() < 2) {
        g_curve = defaultCurve();
    }
    std::sort(g_curve.begin(), g_curve.end(),
              [](const FanPoint& a, const FanPoint& b) { return a.temperatureC < b.temperatureC; });
}

void tick() {
    if (!g_ready) return;
    Telemetry cur;
    std::string err;
    if (g_arc.sampleTelemetry(cur, err)) {
        g_metrics = ArcController::deriveMetrics(g_prev, cur);
        g_prev = cur;
    }
    int pct = -1;
    if (g_arc.fanGetPercent(pct, err)) g_fanPct = pct;
    MemoryInfo mem;
    if (g_arc.readMemory(mem, err)) g_mem = mem;
}

// --- dashboard painting ------------------------------------------------------

struct Tile { std::string label, value, sub; double pct; bool hasPct; };

void drawTile(HDC dc, const RECT& r, const Tile& t) {
    HBRUSH bg = ::CreateSolidBrush(RGB(32, 34, 40));
    ::FillRect(dc, &r, bg);
    ::DeleteObject(bg);
    ::SetBkMode(dc, TRANSPARENT);

    RECT lr = r; lr.left += 14; lr.top += 10;
    ::SetTextColor(dc, RGB(150, 156, 168));
    ::SelectObject(dc, g_fontLabel);
    ::DrawTextW(dc, widen(t.label).c_str(), -1, &lr, DT_LEFT | DT_TOP | DT_SINGLELINE);

    RECT vr = r; vr.left += 14; vr.top += 34;
    ::SetTextColor(dc, RGB(232, 236, 244));
    ::SelectObject(dc, g_fontValue);
    ::DrawTextW(dc, widen(t.value).c_str(), -1, &vr, DT_LEFT | DT_TOP | DT_SINGLELINE);

    if (!t.sub.empty()) {
        RECT sr = r; sr.left += 14; sr.bottom -= 10;
        ::SetTextColor(dc, RGB(150, 156, 168));
        ::SelectObject(dc, g_fontLabel);
        ::DrawTextW(dc, widen(t.sub).c_str(), -1, &sr, DT_LEFT | DT_BOTTOM | DT_SINGLELINE);
    }
    if (t.hasPct) {
        RECT br = r; br.left += 14; br.right -= 14; br.bottom -= 14; br.top = br.bottom - 6;
        HBRUSH track = ::CreateSolidBrush(RGB(52, 55, 63));
        ::FillRect(dc, &br, track); ::DeleteObject(track);
        double p = t.pct; if (p < 0) p = 0; if (p > 100) p = 100;
        RECT fr = br; fr.right = br.left + static_cast<int>((br.right - br.left) * (p / 100.0));
        HBRUSH fill = ::CreateSolidBrush(p >= 90 ? RGB(220, 90, 80) : RGB(80, 150, 220));
        ::FillRect(dc, &fr, fill); ::DeleteObject(fill);
    }
}

void paintDashboard(HDC dc, const RECT& client) {
    const Metrics& m = g_metrics;
    std::vector<Tile> tiles;
    tiles.push_back({"GPU CLOCK", fmt("%.0f", m.gpuFreqMHz) + " MHz", "", 0, false});
    if (m.hasCardPower)
        tiles.push_back({"CARD POWER", fmt("%.0f", m.cardPowerW) + " W",
                         m.hasGpuPower ? fmt("%.0f", m.gpuPowerW) + " W GPU" : "", 0, false});
    tiles.push_back({"GPU TEMP", fmt("%.0f", m.gpuTempC) + " C",
                     m.vramTempC > 0 ? fmt("%.0f", m.vramTempC) + " C VRAM" : "", 0, false});
    if (m.hasGpuUtil)
        tiles.push_back({"GPU UTIL", fmt("%.0f", m.gpuUtilPct) + " %", "", m.gpuUtilPct, true});
    {
        std::string v = (g_fanPct >= 0) ? std::to_string(g_fanPct) + " %" : fmt("%.0f", m.fanRpm) + " RPM";
        std::string sub = (g_fanPct >= 0) ? fmt("%.0f", m.fanRpm) + " RPM" : "";
        tiles.push_back({"FAN", v, sub, (g_fanPct >= 0) ? (double)g_fanPct : 0, g_fanPct >= 0});
    }
    if (g_mem.totalBytes > 0) {
        const double gib = 1024.0 * 1024.0 * 1024.0;
        double pctUsed = 100.0 * g_mem.usedBytes / g_mem.totalBytes;
        tiles.push_back({"VRAM", fmt("%.1f", g_mem.usedBytes / gib) + " GiB",
                         "of " + fmt("%.1f", g_mem.totalBytes / gib) + " GiB", pctUsed, true});
    }
    if (m.gpuVoltageV > 0)
        tiles.push_back({"GPU VOLTAGE", fmt("%.0f", m.gpuVoltageV * 1000.0) + " mV", "", 0, false});
    if (m.hasRenderUtil)
        tiles.push_back({"RENDER UTIL", fmt("%.0f", m.renderUtilPct) + " %", "", m.renderUtilPct, true});
    if (m.hasMediaUtil)
        tiles.push_back({"MEDIA UTIL", fmt("%.0f", m.mediaUtilPct) + " %", "", m.mediaUtilPct, true});
    if (m.hasVramReadBw || m.hasVramWriteBw)
        tiles.push_back({"VRAM BANDWIDTH",
                         fmt("%.1f", (m.vramReadBwMBps + m.vramWriteBwMBps) / 1024.0) + " GB/s",
                         fmt("%.0f", m.vramReadBwMBps) + " R / " + fmt("%.0f", m.vramWriteBwMBps) + " W MB/s",
                         0, false});
    {
        std::string lim;
        if (m.powerLimited)   lim += "power ";
        if (m.tempLimited)    lim += "temp ";
        if (m.voltageLimited) lim += "voltage ";
        if (m.currentLimited) lim += "current ";
        if (m.utilLimited)    lim += "util ";
        tiles.push_back({"THROTTLE", lim.empty() ? "none" : lim, "", 0, false});
    }

    const int top = 96, pad = 12, cols = 3;
    const int tileW = (client.right - 16 * 2 - pad * (cols - 1)) / cols;
    const int tileH = 96;
    for (size_t i = 0; i < tiles.size(); ++i) {
        int c = (int)i % cols, rrow = (int)i / cols;
        RECT tr{16 + c * (tileW + pad), top + rrow * (tileH + pad), 0, 0};
        tr.right = tr.left + tileW; tr.bottom = tr.top + tileH;
        drawTile(dc, tr, tiles[i]);
    }
}

// --- fan-curve editor --------------------------------------------------------

RECT graphRect(const RECT& client) {
    RECT g{60, 100, client.right - 24, client.bottom - 86};   // leave room for the bottom buttons
    return g;
}

POINT curveToScreen(const RECT& g, const FanPoint& p) {
    POINT pt;
    pt.x = g.left + (int)((g.right - g.left) * (p.temperatureC / 100.0));
    pt.y = g.bottom - (int)((g.bottom - g.top) * (p.speedPercent / 100.0));
    return pt;
}

FanPoint screenToCurve(const RECT& g, int x, int y) {
    FanPoint p;
    double t = 100.0 * (x - g.left) / std::max(1L, (long)(g.right - g.left));
    double s = 100.0 * (g.bottom - y) / std::max(1L, (long)(g.bottom - g.top));
    p.temperatureC = (int)std::lround(std::min(100.0, std::max(0.0, t)));
    p.speedPercent = (int)std::lround(std::min(100.0, std::max(0.0, s)));
    return p;
}

int hitNode(const RECT& g, int x, int y) {
    for (size_t i = 0; i < g_curve.size(); ++i) {
        POINT pt = curveToScreen(g, g_curve[i]);
        if (abs(pt.x - x) <= 9 && abs(pt.y - y) <= 9) return (int)i;
    }
    return -1;
}

void paintCurve(HDC dc, const RECT& client) {
    RECT g = graphRect(client);
    HBRUSH panel = ::CreateSolidBrush(RGB(28, 30, 36));
    ::FillRect(dc, &g, panel); ::DeleteObject(panel);
    ::SetBkMode(dc, TRANSPARENT);
    ::SelectObject(dc, g_fontLabel);

    // Grid + axis labels every 20 units.
    HPEN grid = ::CreatePen(PS_SOLID, 1, RGB(48, 51, 59));
    HGDIOBJ oldPen = ::SelectObject(dc, grid);
    ::SetTextColor(dc, RGB(130, 136, 148));
    for (int v = 0; v <= 100; v += 20) {
        int y = g.bottom - (g.bottom - g.top) * v / 100;
        ::MoveToEx(dc, g.left, y, nullptr); ::LineTo(dc, g.right, y);
        RECT lr{g.left - 40, y - 8, g.left - 6, y + 8};
        ::DrawTextW(dc, widen(std::to_string(v) + "%").c_str(), -1, &lr, DT_RIGHT | DT_VCENTER | DT_SINGLELINE);
        int x = g.left + (g.right - g.left) * v / 100;
        ::MoveToEx(dc, x, g.top, nullptr); ::LineTo(dc, x, g.bottom);
        RECT br{x - 20, g.bottom + 4, x + 20, g.bottom + 22};
        ::DrawTextW(dc, widen(std::to_string(v) + "C").c_str(), -1, &br, DT_CENTER | DT_TOP | DT_SINGLELINE);
    }
    ::SelectObject(dc, oldPen); ::DeleteObject(grid);

    // Live GPU-temperature marker.
    if (g_metrics.gpuTempC > 0) {
        int x = g.left + (int)((g.right - g.left) * (std::min(100.0, g_metrics.gpuTempC) / 100.0));
        HPEN mk = ::CreatePen(PS_DOT, 1, RGB(120, 200, 130));
        HGDIOBJ op = ::SelectObject(dc, mk);
        ::MoveToEx(dc, x, g.top, nullptr); ::LineTo(dc, x, g.bottom);
        ::SelectObject(dc, op); ::DeleteObject(mk);
    }

    // Curve polyline.
    HPEN line = ::CreatePen(PS_SOLID, 2, RGB(80, 150, 220));
    oldPen = ::SelectObject(dc, line);
    for (size_t i = 0; i < g_curve.size(); ++i) {
        POINT pt = curveToScreen(g, g_curve[i]);
        if (i == 0) ::MoveToEx(dc, pt.x, pt.y, nullptr); else ::LineTo(dc, pt.x, pt.y);
    }
    ::SelectObject(dc, oldPen); ::DeleteObject(line);

    // Node handles.
    HBRUSH node = ::CreateSolidBrush(RGB(232, 236, 244));
    HGDIOBJ oldBr = ::SelectObject(dc, node);
    for (const FanPoint& p : g_curve) {
        POINT pt = curveToScreen(g, p);
        ::Ellipse(dc, pt.x - 6, pt.y - 6, pt.x + 6, pt.y + 6);
    }
    ::SelectObject(dc, oldBr); ::DeleteObject(node);

    // Hint line (above the bottom button row).
    RECT hint{g.left, client.bottom - 68, client.right - 16, client.bottom - 50};
    ::SetTextColor(dc, RGB(130, 136, 148));
    ::DrawTextW(dc, L"Drag a node to edit    -    double-click to add    -    right-click to remove",
                -1, &hint, DT_LEFT | DT_VCENTER | DT_SINGLELINE);
}

// Padded axis ranges for the OC graph (shared by paint + mouse mapping). The
// frequency axis includes the freq-offset shift so the preview stays on-graph.
void vfRange(int& fmin, int& fmax, int& vmin, int& vmax) {
    uint32_t flo = 0xffffffff, fhi = 0, vlo = 0xffffffff, vhi = 0;
    for (const VFPoint& p : g_vfStock) {
        flo = std::min(flo, p.freqMHz); fhi = std::max(fhi, p.freqMHz);
        vlo = std::min(vlo, p.voltageMv); vhi = std::max(vhi, p.voltageMv);
    }
    const int foff = ocGetPos(g_oc[OC_FREQ]);
    fmin = (int)flo + std::min(0, foff);
    fmax = (int)fhi + std::max(0, foff);
    if (fmax <= fmin) fmax = fmin + 1;
    vmin = (vlo > 40) ? (int)vlo - 40 : 0;
    vmax = (int)vhi + 60;
    if (vmax <= vmin) vmax = vmin + 1;
}

RECT ocGraphRect(const RECT& client) {
    const int top = g_ocGated ? 156 : 108;
    return RECT{16, top, client.right - 16, top + 118};
}

void initVfEdit() {   // full editable curve + ~8 evenly-spaced draggable anchors
    g_vfEdit = g_vfStock;
    g_vfAnchors.clear();
    const int n = (int)g_vfEdit.size();
    if (n < 2) return;
    const int k = std::min(8, n);
    for (int a = 0; a < k; ++a) g_vfAnchors.push_back(a * (n - 1) / (k - 1));
}

void reinterpVf() {   // interpolate voltage between anchors + keep it monotonic
    for (size_t a = 0; a + 1 < g_vfAnchors.size(); ++a) {
        int i0 = g_vfAnchors[a], i1 = g_vfAnchors[a + 1];
        double v0 = g_vfEdit[i0].voltageMv, v1 = g_vfEdit[i1].voltageMv;
        for (int i = i0 + 1; i < i1; ++i)
            g_vfEdit[i].voltageMv = (uint32_t)std::lround(v0 + (v1 - v0) * (double)(i - i0) / (i1 - i0));
    }
    for (size_t i = 1; i < g_vfEdit.size(); ++i)
        if (g_vfEdit[i].voltageMv < g_vfEdit[i - 1].voltageMv)
            g_vfEdit[i].voltageMv = g_vfEdit[i - 1].voltageMv;
}

// Voltage-frequency curve. Offset mode: dashed stock vs solid preview (stock
// shifted by the freq offset on X and voltage offset on Y, clamped to the limit).
// Curve mode: dashed stock vs the editable curve with draggable anchor nodes.
void paintOcGraph(HDC dc, const RECT& g) {
    HBRUSH panel = ::CreateSolidBrush(RGB(28, 30, 36));
    ::FillRect(dc, &g, panel); ::DeleteObject(panel);
    ::SetBkMode(dc, TRANSPARENT);
    ::SelectObject(dc, g_fontLabel);

    if (g_vfStock.size() < 2) {
        ::SetTextColor(dc, RGB(130, 136, 148));
        RECT tr = g;
        ::DrawTextW(dc, g_ocGated ? L"Voltage curve not available on this GPU (firmware-locked)."
                                  : L"Voltage curve unavailable.",
                    -1, &tr, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        return;
    }
    int fmin, fmax, vmin, vmax; vfRange(fmin, fmax, vmin, vmax);
    auto X = [&](int f) { if (f < fmin) f = fmin; if (f > fmax) f = fmax;
                          return g.left + (int)((long long)(g.right - g.left) * (f - fmin) / (fmax - fmin)); };
    auto Y = [&](int mv) { if (mv < vmin) mv = vmin; if (mv > vmax) mv = vmax;
                           return g.bottom - (int)((long long)(g.bottom - g.top) * (mv - vmin) / (vmax - vmin)); };

    const int voff = ocGetPos(g_oc[OC_VOLT]);
    const int foff = ocGetPos(g_oc[OC_FREQ]);
    const int vlim = ocGetPos(g_oc[OC_VLIM]);

    // Stock (dashed grey).
    HPEN sp = ::CreatePen(PS_DOT, 1, RGB(120, 126, 140));
    HGDIOBJ op = ::SelectObject(dc, sp);
    for (size_t i = 0; i < g_vfStock.size(); ++i) {
        POINT pt{X((int)g_vfStock[i].freqMHz), Y((int)g_vfStock[i].voltageMv)};
        if (i == 0) ::MoveToEx(dc, pt.x, pt.y, nullptr); else ::LineTo(dc, pt.x, pt.y);
    }
    ::SelectObject(dc, op); ::DeleteObject(sp);

    // Preview (solid accent).
    HPEN pp = ::CreatePen(PS_SOLID, 2, RGB(80, 150, 220));
    op = ::SelectObject(dc, pp);
    const std::vector<VFPoint>& prev = g_ocCurveMode ? g_vfEdit : g_vfStock;
    for (size_t i = 0; i < prev.size(); ++i) {
        int f = (int)prev[i].freqMHz, mv = (int)prev[i].voltageMv;
        if (!g_ocCurveMode) { f += foff; mv = std::min(mv + voff, vlim); }
        POINT pt{X(f), Y(mv)};
        if (i == 0) ::MoveToEx(dc, pt.x, pt.y, nullptr); else ::LineTo(dc, pt.x, pt.y);
    }
    ::SelectObject(dc, op); ::DeleteObject(pp);

    // Anchor nodes (curve mode).
    if (g_ocCurveMode && !g_vfEdit.empty()) {
        HBRUSH node = ::CreateSolidBrush(RGB(232, 236, 244));
        HGDIOBJ ob = ::SelectObject(dc, node);
        for (int idx : g_vfAnchors) {
            POINT pt{X((int)g_vfEdit[idx].freqMHz), Y((int)g_vfEdit[idx].voltageMv)};
            ::Ellipse(dc, pt.x - 6, pt.y - 6, pt.x + 6, pt.y + 6);
        }
        ::SelectObject(dc, ob); ::DeleteObject(node);
    }

    ::SetTextColor(dc, RGB(130, 136, 148));
    RECT yl{g.left + 6, g.top + 3, g.left + 90, g.top + 19};
    ::DrawTextW(dc, L"voltage (mV)", -1, &yl, DT_LEFT | DT_TOP | DT_SINGLELINE);
    RECT xl{g.right - 96, g.bottom - 18, g.right - 6, g.bottom - 3};
    ::DrawTextW(dc, L"freq (MHz)", -1, &xl, DT_RIGHT | DT_BOTTOM | DT_SINGLELINE);
}

void paintOverclock(HDC dc, const RECT& client) {
    ::SetBkMode(dc, TRANSPARENT);
    ::SelectObject(dc, g_fontLabel);

    if (g_ocGated) {   // banner
        RECT b{16, 106, client.right - 16, 148};
        HBRUSH bb = ::CreateSolidBrush(RGB(74, 54, 30));
        ::FillRect(dc, &b, bb); ::DeleteObject(bb);
        ::SetTextColor(dc, RGB(236, 206, 156));
        RECT bt = b; bt.left += 12; bt.right -= 12;
        ::DrawTextW(dc,
            L"Overclocking is limited on this GPU (B70): voltage, memory and temperature are "
            L"firmware-locked by Intel. Frequency and power limits still apply.",
            -1, &bt, DT_LEFT | DT_VCENTER | DT_WORDBREAK);
    }

    RECT g = ocGraphRect(client);
    paintOcGraph(dc, g);
    if (!g_ocGated) {
        ::SelectObject(dc, g_fontLabel);
        ::SetTextColor(dc, RGB(130, 136, 148));
        RECT mh{g.left, g.bottom + 4, g.right, g.bottom + 20};
        ::DrawTextW(dc, g_ocCurveMode ? L"Manual curve: drag the nodes to shape voltage per frequency."
                                      : L"Offset mode: the sliders shift the whole curve. Tick Manual VF curve to shape it.",
                    -1, &mh, DT_LEFT | DT_VCENTER | DT_SINGLELINE);
    }

    // Slider labels (greyed for gated knobs).
    ::SelectObject(dc, g_fontLabel);
    for (int i = 0; i < kOcCount; ++i) {
        const bool on = (!g_ocGated || !g_oc[i].b70Gated);
        ::SetTextColor(dc, on ? RGB(200, 205, 214) : RGB(108, 112, 120));
        RECT lr{24, (268 + i * 32) + 4, 202, (268 + i * 32) + 24};
        ::DrawTextW(dc, g_oc[i].label, -1, &lr, DT_LEFT | DT_VCENTER | DT_SINGLELINE);
    }
}

void onPaint(HWND hwnd) {
    PAINTSTRUCT ps;
    HDC wdc = ::BeginPaint(hwnd, &ps);
    RECT client; ::GetClientRect(hwnd, &client);

    // Double-buffer: draw everything into a memory bitmap, then blit once. This is
    // what kills the per-second repaint flicker.
    HDC dc = ::CreateCompatibleDC(wdc);
    HBITMAP bmp = ::CreateCompatibleBitmap(wdc, client.right, client.bottom);
    HGDIOBJ oldBmp = ::SelectObject(dc, bmp);

    HBRUSH bg = ::CreateSolidBrush(RGB(20, 21, 25));
    ::FillRect(dc, &client, bg); ::DeleteObject(bg);

    RECT dl{16, 46, client.right - 16, 72};
    ::SetBkMode(dc, TRANSPARENT);
    ::SetTextColor(dc, RGB(200, 205, 214));
    ::SelectObject(dc, g_fontLabel);
    ::DrawTextW(dc, widen(g_ready ? g_deviceLine : ("Not ready: " + g_initError)).c_str(),
                -1, &dl, DT_LEFT | DT_VCENTER | DT_SINGLELINE);

    if (g_ready) {
        if (g_view == View::Dashboard)  paintDashboard(dc, client);
        else if (g_view == View::Fan)   paintCurve(dc, client);
        else                            paintOverclock(dc, client);
    }

    ::BitBlt(wdc, 0, 0, client.right, client.bottom, dc, 0, 0, SRCCOPY);
    ::SelectObject(dc, oldBmp); ::DeleteObject(bmp); ::DeleteDC(dc);
    ::EndPaint(hwnd, &ps);
}

// --- controls ----------------------------------------------------------------

void loadOcFields();   // defined below (used by setView)

void setView(HWND hwnd, View v) {
    g_view = v;
    const bool fan = (v == View::Fan);
    const bool oc  = (v == View::Overclock);
    // Fan section: Auto / Max / Apply / Reset.
    ::ShowWindow(g_btnAuto,       fan ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_btnMax,        fan ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_btnApplyCurve, fan ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_btnResetCurve, fan ? SW_SHOW : SW_HIDE);
    // Overclock section: Apply / Reset / Preset + the sliders.
    ::ShowWindow(g_btnApplyOc, oc ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_btnResetOc, oc ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_cbPreset,   oc ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_chkCurve,   oc ? SW_SHOW : SW_HIDE);
    for (int i = 0; i < kOcCount; ++i) {
        ::ShowWindow(g_oc[i].tb,  oc ? SW_SHOW : SW_HIDE);
        ::ShowWindow(g_oc[i].val, oc ? SW_SHOW : SW_HIDE);
    }
    if (fan) loadEditorCurve();
    if (oc)  loadOcFields();
    ::InvalidateRect(hwnd, nullptr, TRUE);
}

void reselect(HWND hwnd) {
    int idx = (int)::SendMessageW(g_combo, CB_GETCURSEL, 0, 0);
    std::string err;
    if (idx >= 0 && g_arc.selectByIndex((size_t)idx, err)) {
        refreshDeviceLine();
        Telemetry t; g_arc.sampleTelemetry(t, err); g_prev = t;
        if (g_view == View::Fan) loadEditorCurve();
        if (g_view == View::Overclock) loadOcFields();   // re-gate for the new card
        tick();
        ::InvalidateRect(hwnd, nullptr, TRUE);
    }
}

// The ArcFanControl SERVICE (running as SYSTEM) is the SINGLE owner of the fan.
// The GUI only edits the saved profile and signals the service to apply it — it
// NEVER writes IGCL fan state directly. A non-elevated GUI write, or an invalid
// (e.g. non-monotonic) curve, can otherwise silently lock the fan into a
// no-control state until a driver reset.
void nudgeService() {
    HANDLE e = ::OpenEventW(EVENT_MODIFY_STATE, FALSE, L"Global\\ArcFanControlApply");
    if (e) { ::SetEvent(e); ::CloseHandle(e); }
    // If the service isn't running, its next start / 60s cycle picks up the config.
}

// Save the fan portion of the SELECTED adapter's profile and ask the service to
// apply it now. Keyed by the current adapter (PCI device id) so a B70 curve is
// stored for the B70 — not merged onto the B60.
bool saveFanProfile(HWND hwnd, FanMode mode, const std::vector<FanPoint>& curve, int fixedPct) {
    std::string err;
    const AdapterInfo* d = g_arc.current();
    const std::string key = d ? d->key() : std::string();
    AppConfig cfg; loadConfigFor(key, cfg, err);   // preserve this adapter's OC settings
    cfg.fanMode = mode;
    if (mode == FanMode::Curve) cfg.curve = curve;
    if (mode == FanMode::Fixed) cfg.fixedPercent = fixedPct;
    if (!saveConfigFor(key, cfg, err)) {
        ::MessageBoxW(hwnd, widen("Could not save profile: " + err).c_str(), L"Fan", MB_ICONWARNING);
        return false;
    }
    nudgeService();
    return true;
}

void applyCurve(HWND hwnd) {
    saveFanProfile(hwnd, FanMode::Curve, g_curve, 0);
}

// --- overclock tab (sliders + presets + VF graph + B70 gating) ---------------
// (OC_* enum + ocUpdateVal/ocSetPos/ocGetPos are defined up in the globals.)

// Load the selected adapter's OC into the sliders (falling back to live state),
// read the stock VF curve for the graph, and gate the B70-unsupported knobs.
void loadOcFields() {
    std::string err;
    const AdapterInfo* d = g_arc.current();
    const std::string key = d ? d->key() : std::string();
    g_ocGated = (key == "e223");     // B70/G31 firmware rejects the voltage/mem/temp OC ops
    AppConfig cfg; loadConfigFor(key, cfg, err);
    OcState s; const bool live = g_arc.ocGetState(s, err);
    auto val = [&](bool cHas, double cV, bool lHas, double lV, double dflt) {
        if (cHas) return cV; if (lHas) return lV; return dflt;
    };
    ocSetPos(g_oc[OC_FREQ],  (int)std::lround(val(cfg.hasFreqOffset, cfg.freqOffset, live && s.hasGpuFreqOffset, s.gpuFreqOffset, 0)));
    ocSetPos(g_oc[OC_VOLT],  (int)std::lround(val(cfg.hasVoltOffset, cfg.voltOffset, live && s.hasGpuVoltOffset, s.gpuVoltOffset, 0)));
    ocSetPos(g_oc[OC_VLIM],  1200);
    ocSetPos(g_oc[OC_POWER], (int)std::lround(val(cfg.hasPowerW, cfg.powerW, live && s.hasPowerLimit, s.powerLimitW, 190)));
    ocSetPos(g_oc[OC_TEMP],  (int)std::lround(val(cfg.hasTempC, cfg.tempC, live && s.hasTempLimit, s.tempLimitC, 100)));
    ocSetPos(g_oc[OC_MEM],   (int)std::lround(val(cfg.hasMemSpeed, cfg.memSpeed, live && s.hasMemSpeed, s.memSpeed, 19)));

    g_vfStock.clear();
    std::string e2; g_arc.readVFCurve(g_vfStock, false, e2);   // stock curve (empty on gated cards)

    for (int i = 0; i < kOcCount; ++i)
        ::EnableWindow(g_oc[i].tb, (!g_ocGated || !g_oc[i].b70Gated) ? TRUE : FALSE);
    ::EnableWindow(g_cbPreset, g_ocGated ? FALSE : TRUE);

    // Reset to offset mode; seed the editable curve from the (loaded/stock) curve.
    g_ocCurveMode = false;
    if (g_chkCurve) ::SendMessageW(g_chkCurve, BM_SETCHECK, BST_UNCHECKED, 0);
    if (!cfg.vfCurve.empty()) { g_vfEdit = cfg.vfCurve; g_ocCurveMode = true;
                                if (g_chkCurve) ::SendMessageW(g_chkCurve, BM_SETCHECK, BST_CHECKED, 0);
                                // rebuild anchors over the loaded curve
                                g_vfAnchors.clear(); int n=(int)g_vfEdit.size(); int k=std::min(8,n);
                                for (int a=0; n>=2 && a<k; ++a) g_vfAnchors.push_back(a*(n-1)/(k-1)); }
    else initVfEdit();
    ::EnableWindow(g_chkCurve, (g_vfStock.size() >= 2 && !g_ocGated) ? TRUE : FALSE);
    ::EnableWindow(g_oc[OC_VOLT].tb, (!g_ocGated && !g_ocCurveMode) ? TRUE : FALSE);
}

// Save the sliders to the selected adapter's profile + nudge the service.
void applyOc(HWND hwnd) {
    std::string err;
    const AdapterInfo* d = g_arc.current();
    const std::string key = d ? d->key() : std::string();
    AppConfig cfg; loadConfigFor(key, cfg, err);
    cfg.ocApply = true;
    cfg.hasFreqOffset = true; cfg.freqOffset = ocGetPos(g_oc[OC_FREQ]);   // freq + power always
    cfg.hasPowerW     = true; cfg.powerW     = ocGetPos(g_oc[OC_POWER]);
    if (!g_ocGated) {   // voltage / temp / mem only where the firmware allows it
        cfg.hasTempC      = true; cfg.tempC      = ocGetPos(g_oc[OC_TEMP]);
        cfg.hasMemSpeed   = true; cfg.memSpeed   = ocGetPos(g_oc[OC_MEM]);
        if (g_ocCurveMode && g_vfEdit.size() >= 2) {   // manual curve replaces the offset
            cfg.vfCurve = g_vfEdit;
            cfg.hasVoltOffset = false;
        } else {
            cfg.vfCurve.clear();
            cfg.hasVoltOffset = true; cfg.voltOffset = ocGetPos(g_oc[OC_VOLT]);
        }
    } else {
        cfg.hasVoltOffset = cfg.hasTempC = cfg.hasMemSpeed = false;
        cfg.vfCurve.clear();
    }
    if (!saveConfigFor(key, cfg, err)) {
        ::MessageBoxW(hwnd, widen("Could not save overclock: " + err).c_str(), L"Overclock", MB_ICONWARNING);
        return;
    }
    nudgeService();
}

void resetOc(HWND hwnd) {
    ocSetPos(g_oc[OC_FREQ], 0);
    ocSetPos(g_oc[OC_VOLT], 0);
    ocSetPos(g_oc[OC_VLIM], 1200);
    ocSetPos(g_oc[OC_TEMP], 100);
    ocSetPos(g_oc[OC_MEM], 19);
    applyOc(hwnd);
    ::InvalidateRect(hwnd, nullptr, TRUE);
}

// Preset dropdown: [0]="Preset...", then Stock/Efficient/Balanced/Performance
// (mirrors the Linux OC_PRESETS). Loads the sliders; nothing applies until Apply.
void applyPreset(int comboIdx) {
    struct P { int off, vlim, temp, mem; };
    static const P presets[] = {
        {0, 1200, 100, 19}, {-50, 1050, 85, 19}, {-25, 1100, 95, 19}, {25, 1200, 100, 20},
    };
    const int pi = comboIdx - 1;
    if (pi < 0 || pi >= (int)(sizeof(presets) / sizeof(presets[0]))) return;
    const P& p = presets[pi];
    ocSetPos(g_oc[OC_VOLT], p.off);
    ocSetPos(g_oc[OC_VLIM], p.vlim);
    ocSetPos(g_oc[OC_TEMP], p.temp);
    ocSetPos(g_oc[OC_MEM],  p.mem);
}

void createControls(HWND hwnd) {
    RECT cr; ::GetClientRect(hwnd, &cr);
    const int ch = cr.bottom;

    g_combo = ::CreateWindowW(L"COMBOBOX", nullptr,
        WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 16, 10, 290, 220,
        hwnd, (HMENU)(INT_PTR)kIdCombo, nullptr, nullptr);
    for (const AdapterInfo& d : g_arc.adapters())
        ::SendMessageW(g_combo, CB_ADDSTRING, 0, (LPARAM)widen(d.name).c_str());
    ::SendMessageW(g_combo, CB_SETCURSEL, 0, 0);

    // Dark owner-drawn buttons (see drawButton()).
    auto nav = [&](int id, const wchar_t* text, int x, int w) -> HWND {
        return ::CreateWindowW(L"BUTTON", text, WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                               x, 10, w, 28, hwnd, (HMENU)(INT_PTR)id, nullptr, nullptr);
    };
    g_btnDash  = nav(kIdBtnDash,  L"Dashboard", 314, 88);
    g_btnCurve = nav(kIdBtnCurve, L"Fan",       406, 52);
    g_btnOc    = nav(kIdBtnOc,    L"Overclock", 462, 96);

    auto btn = [&](int id, const wchar_t* text, int x, int y, int w) -> HWND {
        return ::CreateWindowW(L"BUTTON", text, WS_CHILD | BS_OWNERDRAW,
                               x, y, w, 28, hwnd, (HMENU)(INT_PTR)id, nullptr, nullptr);
    };
    // Fan action buttons: a row along the BOTTOM of the Fan view (no overlap with
    // the curve, which is drawn above them).
    const int fby = ch - 42;
    g_btnAuto       = btn(kIdBtnAuto,       L"Fan Auto", 16,  fby, 92);
    g_btnMax        = btn(kIdBtnMax,        L"Fan Max",  114, fby, 92);
    g_btnApplyCurve = btn(kIdBtnApplyCurve, L"Apply",    212, fby, 92);
    g_btnResetCurve = btn(kIdBtnResetCurve, L"Reset",    310, fby, 92);

    g_btnApplyOc = btn(kIdBtnApplyOc, L"Apply",          16, 74, 88);
    g_btnResetOc = btn(kIdBtnResetOc, L"Reset to stock", 112, 74, 122);
    g_cbPreset = ::CreateWindowW(L"COMBOBOX", nullptr, WS_CHILD | CBS_DROPDOWNLIST,
                                 236, 74, 170, 240, hwnd, (HMENU)(INT_PTR)kIdCbPreset, nullptr, nullptr);
    for (const wchar_t* p : { L"Preset...", L"Stock", L"Efficient", L"Balanced", L"Performance" })
        ::SendMessageW(g_cbPreset, CB_ADDSTRING, 0, (LPARAM)p);
    ::SendMessageW(g_cbPreset, CB_SETCURSEL, 0, 0);
    g_chkCurve = ::CreateWindowW(L"BUTTON", L"Manual VF curve",
        WS_CHILD | BS_AUTOCHECKBOX, 416, 76, 156, 24, hwnd, (HMENU)(INT_PTR)kIdChkCurve, nullptr, nullptr);
    ::SendMessageW(g_chkCurve, WM_SETFONT, (WPARAM)g_fontLabel, TRUE);

    // OC slider rows: label (painted) + trackbar + value readout.
    const int slY0 = 268, slDY = 32;
    for (int i = 0; i < kOcCount; ++i) {
        int y = slY0 + i * slDY;
        g_oc[i].tb = ::CreateWindowW(L"msctls_trackbar32", L"",
            WS_CHILD | TBS_HORZ | TBS_NOTICKS, 210, y, 260, 26,
            hwnd, (HMENU)(INT_PTR)g_oc[i].id, nullptr, nullptr);
        ::SendMessageW(g_oc[i].tb, TBM_SETRANGEMIN, FALSE, (LPARAM)g_oc[i].lo);
        ::SendMessageW(g_oc[i].tb, TBM_SETRANGEMAX, TRUE,  (LPARAM)g_oc[i].hi);
        g_oc[i].val = ::CreateWindowW(L"STATIC", L"", WS_CHILD, 480, y + 4, 96, 20,
            hwnd, nullptr, nullptr, nullptr);
        ::SendMessageW(g_oc[i].val, WM_SETFONT, (WPARAM)g_fontLabel, TRUE);
    }

    std::vector<HWND> hide = { g_btnAuto, g_btnMax, g_btnApplyCurve, g_btnResetCurve,
                               g_btnApplyOc, g_btnResetOc, g_cbPreset, g_chkCurve };
    for (int i = 0; i < kOcCount; ++i) { hide.push_back(g_oc[i].tb); hide.push_back(g_oc[i].val); }
    for (HWND h : hide) ::ShowWindow(h, SW_HIDE);
}

// --- mouse (fan-curve editing) -----------------------------------------------

void onLDown(HWND hwnd, int x, int y) {
    if (g_view != View::Fan) return;
    RECT client; ::GetClientRect(hwnd, &client);
    g_dragIdx = hitNode(graphRect(client), x, y);
    if (g_dragIdx >= 0) ::SetCapture(hwnd);
}

void onMouseMove(HWND hwnd, int x, int y) {
    if (g_view != View::Fan || g_dragIdx < 0) return;
    RECT client; ::GetClientRect(hwnd, &client);
    FanPoint np = screenToCurve(graphRect(client), x, y);
    // Keep temperatures strictly ordered so the curve stays monotonic in x.
    int lo = (g_dragIdx > 0) ? g_curve[g_dragIdx - 1].temperatureC + 1 : 0;
    int hi = (g_dragIdx + 1 < (int)g_curve.size()) ? g_curve[g_dragIdx + 1].temperatureC - 1 : 100;
    np.temperatureC = std::min(hi, std::max(lo, np.temperatureC));
    g_curve[g_dragIdx] = np;
    ::InvalidateRect(hwnd, nullptr, FALSE);
}

void onLUp(HWND) {
    if (g_dragIdx >= 0) { g_dragIdx = -1; ::ReleaseCapture(); }
}

// --- mouse (OC voltage-curve editing, manual mode) ---------------------------

int ocHitAnchor(const RECT& g, int x, int y) {
    if (g_vfEdit.empty()) return -1;
    int fmin, fmax, vmin, vmax; vfRange(fmin, fmax, vmin, vmax);
    for (size_t a = 0; a < g_vfAnchors.size(); ++a) {
        const VFPoint& p = g_vfEdit[g_vfAnchors[a]];
        int px = g.left + (int)((long long)(g.right - g.left) * ((int)p.freqMHz - fmin) / (fmax - fmin));
        int py = g.bottom - (int)((long long)(g.bottom - g.top) * ((int)p.voltageMv - vmin) / (vmax - vmin));
        if (abs(px - x) <= 9 && abs(py - y) <= 9) return (int)a;
    }
    return -1;
}

void onOcDown(HWND hwnd, int x, int y) {
    if (g_view != View::Overclock || !g_ocCurveMode || g_ocGated) return;
    RECT client; ::GetClientRect(hwnd, &client);
    g_vfDrag = ocHitAnchor(ocGraphRect(client), x, y);
    if (g_vfDrag >= 0) ::SetCapture(hwnd);
}

void onOcMove(HWND hwnd, int x, int y) {
    if (g_view != View::Overclock || g_vfDrag < 0) return;
    RECT client; ::GetClientRect(hwnd, &client);
    RECT g = ocGraphRect(client);
    int fmin, fmax, vmin, vmax; vfRange(fmin, fmax, vmin, vmax);
    double frac = (double)(g.bottom - y) / std::max(1L, (long)(g.bottom - g.top));
    int mv = vmin + (int)std::lround(frac * (vmax - vmin));
    mv = std::max(vmin, std::min(vmax, mv));
    g_vfEdit[g_vfAnchors[g_vfDrag]].voltageMv = (uint32_t)mv;
    reinterpVf();
    ::InvalidateRect(hwnd, nullptr, FALSE);
}

void onOcUp(HWND) {
    if (g_vfDrag >= 0) { g_vfDrag = -1; ::ReleaseCapture(); }
}

void onDblClick(HWND hwnd, int x, int y) {
    if (g_view != View::Fan) return;
    RECT client; ::GetClientRect(hwnd, &client);
    RECT g = graphRect(client);
    if (x < g.left || x > g.right || y < g.top || y > g.bottom) return;
    FanPoint np = screenToCurve(g, x, y);
    g_curve.push_back(np);
    std::sort(g_curve.begin(), g_curve.end(),
              [](const FanPoint& a, const FanPoint& b) { return a.temperatureC < b.temperatureC; });
    ::InvalidateRect(hwnd, nullptr, FALSE);
}

void onRDown(HWND hwnd, int x, int y) {
    if (g_view != View::Fan) return;
    RECT client; ::GetClientRect(hwnd, &client);
    int idx = hitNode(graphRect(client), x, y);
    if (idx >= 0 && g_curve.size() > 2) {
        g_curve.erase(g_curve.begin() + idx);
        ::InvalidateRect(hwnd, nullptr, FALSE);
    }
}

// Dark, rounded owner-drawn button. The active nav tab is highlighted in accent.
void drawButton(const DRAWITEMSTRUCT* d) {
    HDC dc = d->hDC;
    RECT r = d->rcItem;
    const bool pressed  = (d->itemState & ODS_SELECTED) != 0;
    const bool disabled = (d->itemState & ODS_DISABLED) != 0;
    const int id = (int)d->CtlID;
    const bool activeNav =
        (id == kIdBtnDash  && g_view == View::Dashboard) ||
        (id == kIdBtnCurve && g_view == View::Fan) ||
        (id == kIdBtnOc    && g_view == View::Overclock);

    COLORREF fill = activeNav ? RGB(58, 108, 190)
                  : pressed   ? RGB(46, 50, 60)
                              : RGB(40, 43, 52);
    HBRUSH b = ::CreateSolidBrush(fill);
    HPEN pen = ::CreatePen(PS_SOLID, 1, activeNav ? RGB(90, 140, 220) : RGB(64, 68, 80));
    HGDIOBJ ob = ::SelectObject(dc, b), op = ::SelectObject(dc, pen);
    ::RoundRect(dc, r.left, r.top, r.right, r.bottom, 8, 8);
    ::SelectObject(dc, ob); ::SelectObject(dc, op);
    ::DeleteObject(b); ::DeleteObject(pen);

    wchar_t txt[64]; ::GetWindowTextW(d->hwndItem, txt, 64);
    ::SetBkMode(dc, TRANSPARENT);
    ::SetTextColor(dc, disabled ? RGB(120, 124, 132)
                       : activeNav ? RGB(244, 247, 252) : RGB(224, 228, 236));
    ::SelectObject(dc, g_fontLabel);
    ::DrawTextW(dc, txt, -1, &r, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_CREATE:
            g_ready = g_arc.init(g_initError);
            if (g_ready) {
                refreshDeviceLine();
                createControls(hwnd);
                Telemetry t; std::string e; g_arc.sampleTelemetry(t, e); g_prev = t;
                tick();
            }
            ::SetTimer(hwnd, kTimerId, 1000, nullptr);
            addTrayIcon(hwnd);
            return 0;
        case WM_TRAYICON:
            if (LOWORD(lp) == WM_LBUTTONUP || LOWORD(lp) == WM_LBUTTONDBLCLK)
                showMainWindow(hwnd);
            else if (LOWORD(lp) == WM_RBUTTONUP)
                showTrayMenu(hwnd);
            return 0;
        case WM_SYSCOMMAND:
            if ((wp & 0xFFF0) == SC_MINIMIZE) { ::ShowWindow(hwnd, SW_HIDE); return 0; }
            return ::DefWindowProcW(hwnd, msg, wp, lp);
        case WM_CLOSE:
            if (!g_reallyExit) { ::ShowWindow(hwnd, SW_HIDE); return 0; }  // hide to tray
            ::DestroyWindow(hwnd);
            return 0;
        case WM_TIMER:
            tick();
            // Only the live temp marker changes in curve view; repaint both.
            ::InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        case WM_COMMAND: {
            const int id = LOWORD(wp);
            if (id == kIdCombo && HIWORD(wp) == CBN_SELCHANGE) { reselect(hwnd); return 0; }
            if (id == kIdCbPreset && HIWORD(wp) == CBN_SELCHANGE) {
                applyPreset((int)::SendMessageW(g_cbPreset, CB_GETCURSEL, 0, 0));
                ::InvalidateRect(hwnd, nullptr, FALSE); return 0;
            }
            if (id == kIdBtnDash)  { setView(hwnd, View::Dashboard); return 0; }
            if (id == kIdBtnCurve) { setView(hwnd, View::Fan); return 0; }
            if (id == kIdBtnOc)    { setView(hwnd, View::Overclock); return 0; }
            if (id == kIdBtnAuto)  { saveFanProfile(hwnd, FanMode::Auto, {}, 0); return 0; }
            if (id == kIdBtnMax)   { saveFanProfile(hwnd, FanMode::Max, {}, 0); return 0; }
            if (id == kIdBtnApplyCurve) { applyCurve(hwnd); return 0; }
            if (id == kIdBtnResetCurve) { g_curve = defaultCurve(); ::InvalidateRect(hwnd, nullptr, TRUE); return 0; }
            if (id == kIdBtnApplyOc) { applyOc(hwnd); return 0; }
            if (id == kIdBtnResetOc) { resetOc(hwnd); return 0; }
            if (id == kIdChkCurve) {
                g_ocCurveMode = (::SendMessageW(g_chkCurve, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_ocCurveMode && g_vfEdit.size() < 2) initVfEdit();
                ::EnableWindow(g_oc[OC_VOLT].tb, (!g_ocGated && !g_ocCurveMode) ? TRUE : FALSE);
                ::InvalidateRect(hwnd, nullptr, FALSE); return 0;
            }
            if (id == kIdTrayOpen) { showMainWindow(hwnd); return 0; }
            if (id == kIdTrayFanAuto) { saveFanProfile(hwnd, FanMode::Auto, {}, 0); return 0; }
            if (id == kIdTrayFanMax) { saveFanProfile(hwnd, FanMode::Max, {}, 0); return 0; }
            if (id == kIdTrayExit) { g_reallyExit = true; ::DestroyWindow(hwnd); return 0; }
            return 0;
        }
        case WM_HSCROLL: {   // an OC trackbar moved -> update its readout + VF preview
            HWND tb = (HWND)lp;
            for (int i = 0; i < kOcCount; ++i) {
                if (g_oc[i].tb == tb) { ocUpdateVal(g_oc[i]); ::InvalidateRect(hwnd, nullptr, FALSE); break; }
            }
            return 0;
        }
        case WM_LBUTTONDOWN:    onLDown(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp));
                                onOcDown(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_MOUSEMOVE:      onMouseMove(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp));
                                onOcMove(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_LBUTTONUP:      onLUp(hwnd); onOcUp(hwnd); return 0;
        case WM_LBUTTONDBLCLK:  onDblClick(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_RBUTTONDOWN:    onRDown(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_DRAWITEM:       drawButton((const DRAWITEMSTRUCT*)lp); return TRUE;
        case WM_CTLCOLORSTATIC:
        case WM_CTLCOLORBTN: {      // dark bg for the OC value read-outs + checkbox
            ::SetBkMode((HDC)wp, TRANSPARENT);
            ::SetTextColor((HDC)wp, RGB(210, 214, 222));
            static HBRUSH s_dark = ::CreateSolidBrush(RGB(20, 21, 25));
            return (LRESULT)s_dark;
        }
        case WM_ERASEBKGND:     return 1;   // onPaint fully repaints (double-buffered)
        case WM_PAINT:          onPaint(hwnd); return 0;
        case WM_DESTROY:
            ::KillTimer(hwnd, kTimerId);
            removeTrayIcon();
            ::PostQuitMessage(0);
            return 0;
    }
    return ::DefWindowProcW(hwnd, msg, wp, lp);
}

} // namespace

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE, LPWSTR lpCmdLine, int nShow) {
    // "--tray" (used by the login auto-start entry): start hidden, tray only.
    const bool startInTray = lpCmdLine && ::wcsstr(lpCmdLine, L"tray") != nullptr;
    INITCOMMONCONTROLSEX icc{sizeof(icc), ICC_STANDARD_CLASSES | ICC_BAR_CLASSES};
    ::InitCommonControlsEx(&icc);

    g_fontLabel = ::CreateFontW(-12, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    g_fontValue = ::CreateFontW(-26, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");

    WNDCLASSW wc{};
    wc.style = CS_DBLCLKS;
    wc.lpfnWndProc = WndProc;
    wc.hInstance = hInst;
    wc.hCursor = ::LoadCursor(nullptr, IDC_ARROW);
    wc.lpszClassName = L"ArcGpuGuiWindow";
    ::RegisterClassW(&wc);

    HWND hwnd = ::CreateWindowW(wc.lpszClassName, L"Arc GPU Dashboard",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_CLIPCHILDREN,
        CW_USEDEFAULT, CW_USEDEFAULT, 780, 630, nullptr, nullptr, hInst, nullptr);
    if (!hwnd) return 1;

    ::ShowWindow(hwnd, startInTray ? SW_HIDE : nShow);
    ::UpdateWindow(hwnd);

    MSG m;
    while (::GetMessageW(&m, nullptr, 0, 0)) {
        ::TranslateMessage(&m);
        ::DispatchMessageW(&m);
    }

    ::DeleteObject(g_fontLabel);
    ::DeleteObject(g_fontValue);
    return 0;
}
