// arc-gpu-gui — native Win32 dashboard + fan-curve editor for Intel Arc (IGCL).
//
// A self-contained GDI app over arc_core — no external UI toolkit, builds with
// the same MSVC toolchain as the CLI/service. Windows analogue of the Linux GTK4
// `xe-gpu-gui`, with two views:
//
//   Dashboard   live (1 s) clocks / power / temp / utilisation / fan / VRAM,
//               a GPU selector, and Fan Auto / Fan Max buttons.
//   Fan Curve   a draggable temperature->percent curve editor: drag nodes,
//               double-click to add a point, right-click a node to remove it,
//               a live GPU-temperature marker, and Apply / Reset.
//
// The VF-curve / overclock editor tab is not ported yet — use `arc-gpu oc`.
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

enum class View { Dashboard, FanCurve };

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
HWND  g_btnDash = nullptr, g_btnCurve = nullptr;
HWND  g_btnAuto = nullptr, g_btnMax = nullptr;
HWND  g_btnApplyCurve = nullptr, g_btnResetCurve = nullptr;

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
    RECT g{60, 100, client.right - 24, client.bottom - 40};
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

    // Hint line.
    RECT hint{g.left, client.bottom - 22, client.right - 16, client.bottom - 4};
    ::SetTextColor(dc, RGB(130, 136, 148));
    ::DrawTextW(dc, L"Drag a node to edit  ·  double-click to add  ·  right-click to remove",
                -1, &hint, DT_LEFT | DT_VCENTER | DT_SINGLELINE);
}

void onPaint(HWND hwnd) {
    PAINTSTRUCT ps;
    HDC dc = ::BeginPaint(hwnd, &ps);
    RECT client; ::GetClientRect(hwnd, &client);
    HBRUSH bg = ::CreateSolidBrush(RGB(20, 21, 25));
    ::FillRect(dc, &client, bg); ::DeleteObject(bg);

    RECT dl{16, 46, client.right - 16, 72};
    ::SetBkMode(dc, TRANSPARENT);
    ::SetTextColor(dc, RGB(200, 205, 214));
    ::SelectObject(dc, g_fontLabel);
    ::DrawTextW(dc, widen(g_ready ? g_deviceLine : ("Not ready: " + g_initError)).c_str(),
                -1, &dl, DT_LEFT | DT_VCENTER | DT_SINGLELINE);

    if (g_ready) {
        if (g_view == View::Dashboard) paintDashboard(dc, client);
        else                           paintCurve(dc, client);
    }
    ::EndPaint(hwnd, &ps);
}

// --- controls ----------------------------------------------------------------

void setView(HWND hwnd, View v) {
    g_view = v;
    const bool dash = (v == View::Dashboard);
    ::ShowWindow(g_btnAuto, dash ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_btnMax, dash ? SW_SHOW : SW_HIDE);
    ::ShowWindow(g_btnApplyCurve, dash ? SW_HIDE : SW_SHOW);
    ::ShowWindow(g_btnResetCurve, dash ? SW_HIDE : SW_SHOW);
    if (!dash) loadEditorCurve();
    ::InvalidateRect(hwnd, nullptr, TRUE);
}

void reselect(HWND hwnd) {
    int idx = (int)::SendMessageW(g_combo, CB_GETCURSEL, 0, 0);
    std::string err;
    if (idx >= 0 && g_arc.selectByIndex((size_t)idx, err)) {
        refreshDeviceLine();
        Telemetry t; g_arc.sampleTelemetry(t, err); g_prev = t;
        if (g_view == View::FanCurve) loadEditorCurve();
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

void createControls(HWND hwnd) {
    g_combo = ::CreateWindowW(L"COMBOBOX", nullptr,
        WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 16, 10, 300, 220,
        hwnd, (HMENU)(INT_PTR)kIdCombo, nullptr, nullptr);
    for (const AdapterInfo& d : g_arc.adapters())
        ::SendMessageW(g_combo, CB_ADDSTRING, 0, (LPARAM)widen(d.name).c_str());
    ::SendMessageW(g_combo, CB_SETCURSEL, 0, 0);

    auto mk = [&](int id, const wchar_t* text, int x, int w) -> HWND {
        return ::CreateWindowW(L"BUTTON", text, WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                               x, 10, w, 26, hwnd, (HMENU)(INT_PTR)id, nullptr, nullptr);
    };
    g_btnDash = mk(kIdBtnDash, L"Dashboard", 324, 92);
    g_btnCurve = mk(kIdBtnCurve, L"Fan Curve", 420, 92);
    g_btnAuto = mk(kIdBtnAuto, L"Fan Auto", 540, 90);
    g_btnMax = mk(kIdBtnMax, L"Fan Max", 634, 90);
    g_btnApplyCurve = mk(kIdBtnApplyCurve, L"Apply Curve", 540, 110);
    g_btnResetCurve = mk(kIdBtnResetCurve, L"Reset", 654, 70);
    ::ShowWindow(g_btnApplyCurve, SW_HIDE);
    ::ShowWindow(g_btnResetCurve, SW_HIDE);
}

// --- mouse (fan-curve editing) -----------------------------------------------

void onLDown(HWND hwnd, int x, int y) {
    if (g_view != View::FanCurve) return;
    RECT client; ::GetClientRect(hwnd, &client);
    g_dragIdx = hitNode(graphRect(client), x, y);
    if (g_dragIdx >= 0) ::SetCapture(hwnd);
}

void onMouseMove(HWND hwnd, int x, int y) {
    if (g_view != View::FanCurve || g_dragIdx < 0) return;
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

void onDblClick(HWND hwnd, int x, int y) {
    if (g_view != View::FanCurve) return;
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
    if (g_view != View::FanCurve) return;
    RECT client; ::GetClientRect(hwnd, &client);
    int idx = hitNode(graphRect(client), x, y);
    if (idx >= 0 && g_curve.size() > 2) {
        g_curve.erase(g_curve.begin() + idx);
        ::InvalidateRect(hwnd, nullptr, FALSE);
    }
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
            if (id == kIdBtnDash)  { setView(hwnd, View::Dashboard); return 0; }
            if (id == kIdBtnCurve) { setView(hwnd, View::FanCurve); return 0; }
            if (id == kIdBtnAuto)  { saveFanProfile(hwnd, FanMode::Auto, {}, 0); return 0; }
            if (id == kIdBtnMax)   { saveFanProfile(hwnd, FanMode::Max, {}, 0); return 0; }
            if (id == kIdBtnApplyCurve) { applyCurve(hwnd); return 0; }
            if (id == kIdBtnResetCurve) { g_curve = defaultCurve(); ::InvalidateRect(hwnd, nullptr, TRUE); return 0; }
            if (id == kIdTrayOpen) { showMainWindow(hwnd); return 0; }
            if (id == kIdTrayFanAuto) { saveFanProfile(hwnd, FanMode::Auto, {}, 0); return 0; }
            if (id == kIdTrayFanMax) { saveFanProfile(hwnd, FanMode::Max, {}, 0); return 0; }
            if (id == kIdTrayExit) { g_reallyExit = true; ::DestroyWindow(hwnd); return 0; }
            return 0;
        }
        case WM_LBUTTONDOWN:    onLDown(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_MOUSEMOVE:      onMouseMove(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_LBUTTONUP:      onLUp(hwnd); return 0;
        case WM_LBUTTONDBLCLK:  onDblClick(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
        case WM_RBUTTONDOWN:    onRDown(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp)); return 0;
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
    INITCOMMONCONTROLSEX icc{sizeof(icc), ICC_STANDARD_CLASSES};
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
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 780, 560, nullptr, nullptr, hInst, nullptr);
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
