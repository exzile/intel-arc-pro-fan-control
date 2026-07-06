// arc-gpu-gui — native Win32 dashboard for Intel Arc (IGCL backend).
//
// A self-contained GDI window over arc_core — no external UI toolkit, builds
// with the same MSVC toolchain as the CLI/service. It is the Windows analogue of
// the Linux GTK4 `xe-gpu-gui` dashboard tab: a live (1 s) view of clocks, power,
// temperature, utilisation, fan and VRAM, with a GPU selector for multi-card
// boxes and Fan Auto / Fan Max / Apply-profile buttons.
//
// The draggable fan-curve editor and OC tab from the Linux GUI are not ported
// yet; use the `arc-gpu` CLI for those. See windows/PORT.md.
#include <windows.h>
#include <commctrl.h>
#include <cstdio>
#include <string>
#include <vector>

#include "../arc.hpp"
#include "../config.hpp"
#include "../apply.hpp"

#pragma comment(lib, "comctl32.lib")

using namespace arc;

namespace {

enum : int {
    kIdCombo = 1001,
    kIdBtnAuto = 1002,
    kIdBtnMax = 1003,
    kIdBtnApply = 1004,
    kTimerId = 1,
};

ArcController g_arc;
bool          g_ready = false;
std::string   g_initError;

Telemetry  g_prev;              // previous sample, for rate derivation
Metrics    g_metrics;           // latest derived metrics
int        g_fanPct = -1;
MemoryInfo g_mem;
std::string g_deviceLine;

HFONT g_fontLabel = nullptr;
HFONT g_fontValue = nullptr;
HWND  g_combo = nullptr;

std::wstring widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring w(n ? n - 1 : 0, L'\0');
    if (n) ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
    return w;
}

void refreshDeviceLine() {
    if (const AdapterInfo* d = g_arc.current())
        g_deviceLine = d->name + "  (" + d->bdfString() + ")";
    else
        g_deviceLine = "(no adapter)";
}

// Take one telemetry sample and derive against the previous one.
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

// --- painting ----------------------------------------------------------------

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
        ::FillRect(dc, &br, track);
        ::DeleteObject(track);
        double p = t.pct; if (p < 0) p = 0; if (p > 100) p = 100;
        RECT fr = br; fr.right = br.left + static_cast<int>((br.right - br.left) * (p / 100.0));
        HBRUSH fill = ::CreateSolidBrush(p >= 90 ? RGB(220, 90, 80) : RGB(80, 150, 220));
        ::FillRect(dc, &fr, fill);
        ::DeleteObject(fill);
    }
}

std::string fmt(const char* f, double v) {
    char b[64]; std::snprintf(b, sizeof(b), f, v); return b;
}

void onPaint(HWND hwnd) {
    PAINTSTRUCT ps;
    HDC dc = ::BeginPaint(hwnd, &ps);

    RECT client; ::GetClientRect(hwnd, &client);
    HBRUSH bg = ::CreateSolidBrush(RGB(20, 21, 25));
    ::FillRect(dc, &client, bg);
    ::DeleteObject(bg);

    // Device line under the control strip.
    RECT dl{16, 44, client.right - 16, 68};
    ::SetBkMode(dc, TRANSPARENT);
    ::SetTextColor(dc, RGB(200, 205, 214));
    ::SelectObject(dc, g_fontLabel);
    ::DrawTextW(dc, widen(g_ready ? g_deviceLine : ("Not ready: " + g_initError)).c_str(),
                -1, &dl, DT_LEFT | DT_VCENTER | DT_SINGLELINE);

    if (!g_ready) { ::EndPaint(hwnd, &ps); return; }

    // Build tiles.
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
        tiles.push_back({"VRAM",
                         fmt("%.1f", g_mem.usedBytes / gib) + " GiB",
                         "of " + fmt("%.1f", g_mem.totalBytes / gib) + " GiB",
                         pctUsed, true});
    }

    // Grid layout.
    const int top = 80, pad = 12, cols = 3;
    const int tileW = (client.right - 16 * 2 - pad * (cols - 1)) / cols;
    const int tileH = 96;
    for (size_t i = 0; i < tiles.size(); ++i) {
        int c = (int)i % cols, rrow = (int)i / cols;
        RECT tr;
        tr.left = 16 + c * (tileW + pad);
        tr.top = top + rrow * (tileH + pad);
        tr.right = tr.left + tileW;
        tr.bottom = tr.top + tileH;
        drawTile(dc, tr, tiles[i]);
    }

    ::EndPaint(hwnd, &ps);
}

// --- controls / commands -----------------------------------------------------

void reselect(HWND hwnd) {
    int idx = (int)::SendMessageW(g_combo, CB_GETCURSEL, 0, 0);
    std::string err;
    if (idx >= 0 && g_arc.selectByIndex((size_t)idx, err)) {
        refreshDeviceLine();
        Telemetry t; g_arc.sampleTelemetry(t, err); g_prev = t;   // reset baseline
        tick();
        ::InvalidateRect(hwnd, nullptr, FALSE);
    }
}

void doFan(HWND hwnd, bool maxMode) {
    std::string err;
    bool ok = maxMode ? g_arc.fanSetFixed(100, err) : g_arc.fanSetAuto(err);
    if (!ok) ::MessageBoxW(hwnd, widen(err).c_str(), L"Fan", MB_ICONWARNING);
}

void doApply(HWND hwnd) {
    AppConfig cfg; std::string err;
    loadConfig(cfg, err);
    if (!applyProfile(g_arc, cfg, err))
        ::MessageBoxW(hwnd, widen("Applied with warnings: " + err).c_str(), L"Apply", MB_ICONINFORMATION);
}

void createControls(HWND hwnd) {
    g_combo = ::CreateWindowW(L"COMBOBOX", nullptr,
        WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 16, 10, 340, 200,
        hwnd, (HMENU)(INT_PTR)kIdCombo, nullptr, nullptr);
    for (const AdapterInfo& d : g_arc.adapters())
        ::SendMessageW(g_combo, CB_ADDSTRING, 0, (LPARAM)widen(d.name).c_str());
    ::SendMessageW(g_combo, CB_SETCURSEL, 0, 0);

    auto mkBtn = [&](int id, const wchar_t* text, int x) {
        ::CreateWindowW(L"BUTTON", text, WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                        x, 10, 96, 26, hwnd, (HMENU)(INT_PTR)id, nullptr, nullptr);
    };
    mkBtn(kIdBtnAuto, L"Fan Auto", 368);
    mkBtn(kIdBtnMax, L"Fan Max", 470);
    mkBtn(kIdBtnApply, L"Apply", 572);
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_CREATE: {
            g_ready = g_arc.init(g_initError);
            if (g_ready) {
                refreshDeviceLine();
                createControls(hwnd);
                Telemetry t; std::string e; g_arc.sampleTelemetry(t, e); g_prev = t;
                tick();
            }
            ::SetTimer(hwnd, kTimerId, 1000, nullptr);
            return 0;
        }
        case WM_TIMER:
            tick();
            ::InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        case WM_COMMAND: {
            const int id = LOWORD(wp);
            if (id == kIdCombo && HIWORD(wp) == CBN_SELCHANGE) { reselect(hwnd); return 0; }
            if (id == kIdBtnAuto)  { doFan(hwnd, false); return 0; }
            if (id == kIdBtnMax)   { doFan(hwnd, true);  return 0; }
            if (id == kIdBtnApply) { doApply(hwnd); return 0; }
            return 0;
        }
        case WM_PAINT:
            onPaint(hwnd);
            return 0;
        case WM_DESTROY:
            ::KillTimer(hwnd, kTimerId);
            ::PostQuitMessage(0);
            return 0;
    }
    return ::DefWindowProcW(hwnd, msg, wp, lp);
}

} // namespace

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE, LPWSTR, int nShow) {
    INITCOMMONCONTROLSEX icc{sizeof(icc), ICC_STANDARD_CLASSES};
    ::InitCommonControlsEx(&icc);

    g_fontLabel = ::CreateFontW(-12, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    g_fontValue = ::CreateFontW(-26, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");

    WNDCLASSW wc{};
    wc.lpfnWndProc = WndProc;
    wc.hInstance = hInst;
    wc.hCursor = ::LoadCursor(nullptr, IDC_ARROW);
    wc.lpszClassName = L"ArcGpuGuiWindow";
    ::RegisterClassW(&wc);

    HWND hwnd = ::CreateWindowW(wc.lpszClassName, L"Arc GPU Dashboard",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 700, 460, nullptr, nullptr, hInst, nullptr);
    if (!hwnd) return 1;

    ::ShowWindow(hwnd, nShow);
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
