// service_main.cpp — ArcFanControl Windows service.
//
// The Windows analogue of the systemd boot services (xe-fan-curve.service,
// xe-gpu-oc.service). Intel Arc resets to stock fan/overclock on cold boot and
// on driver resets (TDR/resume), so this service:
//
//   1. applies the saved profile (%ProgramData%\ArcFanControl\config.ini) at
//      startup, and
//   2. re-applies it periodically, so a driver reset or resume-from-sleep that
//      wipes the fan curve / overclock is silently restored.
//
// Usage (run elevated):
//   arc-fan-service install     register + auto-start the service
//   arc-fan-service uninstall   stop + remove the service
//   arc-fan-service run         run in the foreground (debugging)
//   (no args when launched by the Service Control Manager)
#include <windows.h>
#include <sddl.h>
#include <cstdio>
#include <memory>
#include <string>

#include "../arc.hpp"
#include "../config.hpp"
#include "../apply.hpp"

#pragma comment(lib, "advapi32.lib")

using namespace arc;

namespace {

const wchar_t* kServiceName = L"ArcFanControl";
const wchar_t* kDisplayName = L"Arc Fan Control Service";

// Named event the (non-elevated) GUI signals after it saves a new profile, so the
// SYSTEM service re-applies it immediately. Global\ so it crosses sessions; a
// permissive DACL lets a standard-user GUI open + signal it. This keeps the
// SERVICE the single fan owner — the GUI never writes IGCL directly (a
// non-elevated / invalid write there can silently lock the fan).
const wchar_t* kApplyEventName = L"Global\\ArcFanControlApply";

SERVICE_STATUS        g_status{};
SERVICE_STATUS_HANDLE g_statusHandle = nullptr;
HANDLE                g_stopEvent = nullptr;
HANDLE                g_applyEvent = nullptr;

// Create the apply event with a DACL granting Everyone EVENT_MODIFY_STATE (so the
// non-elevated GUI can SetEvent it). Returns nullptr on failure (non-fatal).
HANDLE createApplyEvent() {
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = FALSE;
    // D: DACL; (A;;0x0002;;;WD) = Allow EVENT_MODIFY_STATE to Everyone (World).
    if (!::ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:(A;;0x0002;;;WD)(A;;GA;;;SY)(A;;GA;;;BA)", SDDL_REVISION_1,
            &sa.lpSecurityDescriptor, nullptr)) {
        return ::CreateEventW(nullptr, FALSE, FALSE, kApplyEventName);
    }
    HANDLE h = ::CreateEventW(&sa, FALSE /*auto-reset*/, FALSE, kApplyEventName);
    ::LocalFree(sa.lpSecurityDescriptor);
    return h;
}

// Re-apply cadence. Long enough to be unobtrusive, short enough to restore the
// profile promptly after a driver reset / resume.
constexpr DWORD kReapplyIntervalMs = 60'000;

// Cold boot: the service can start before the GPU driver has finished
// initializing (IGCL then returns NOT_READY-class errors and every fan/OC write
// fails). Retry quickly until the first successful apply, then settle to the
// normal cadence.
constexpr DWORD kBootRetryMs = 5'000;

void logLine(const std::string& msg) {
    ::OutputDebugStringA(("[ArcFanControl] " + msg + "\n").c_str());
    std::string e;
    if (!ensureConfigDir(e)) return;
    if (FILE* f = std::fopen((configDir() + "\\service.log").c_str(), "a")) {
        SYSTEMTIME st;
        ::GetLocalTime(&st);
        std::fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d  %s\n", st.wYear, st.wMonth,
                     st.wDay, st.wHour, st.wMinute, st.wSecond, msg.c_str());
        std::fclose(f);
    }
}

void setState(DWORD state, DWORD exitCode = NO_ERROR, DWORD waitHint = 0) {
    g_status.dwCurrentState = state;
    g_status.dwWin32ExitCode = exitCode;
    g_status.dwWaitHint = waitHint;
    g_status.dwControlsAccepted =
        (state == SERVICE_START_PENDING) ? 0 : SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN;
    if (g_statusHandle) ::SetServiceStatus(g_statusHandle, &g_status);
}

// Load config + apply once, reusing a persistent controller. Re-creating the
// controller (and thus ctlClose + Level Zero teardown) every pass crashed the
// process, so init is done ONCE in runLoop and the handle is held for the
// service lifetime. Returns false if the apply failed (handle may be stale).
bool applyOnce(ArcController& a) {
    MultiConfig cfg;
    std::string err;
    loadAllConfigs(cfg, err);

    bool anyProfile = false;
    bool allFansOk = true;      // false only if a FAN failed (driver not ready)
    int applied = 0;
    std::string warnings;

    // Apply EACH adapter's own profile (multi-GPU). Profiles are keyed by PCI
    // device id; find() falls back to the [adapter.default] profile.
    for (size_t i = 0; i < a.adapters().size(); ++i) {
        const std::string key = a.adapters()[i].key();
        const AppConfig* p = cfg.find(key);
        if (!p || (p->fanMode == FanMode::None && !p->ocApply)) continue;
        anyProfile = true;

        std::string sel;
        if (!a.selectByIndex(i, sel)) { allFansOk = false; warnings += "[" + key + "] select: " + sel + "; "; continue; }

        std::string applyErr;
        bool fanOk = true;
        if (applyProfile(a, *p, applyErr, &fanOk)) ++applied;
        else warnings += "[" + key + "] " + applyErr;   // e.g. gated B70 OC — tolerated
        if (!fanOk) allFansOk = false;                   // a failed fan => not ready
    }

    if (!anyProfile) { logLine("no saved profile to apply"); return true; }
    if (warnings.empty()) logLine("profiles applied (" + std::to_string(applied) + " adapter(s))");
    else                  logLine("profiles applied with warnings: " + warnings);

    // Only re-init/retry when a FAN failed (cold-boot not-ready); an expected OC
    // failure on the firmware-gated B70 must not cause an endless retry loop.
    return allFansOk;
}

DWORD WINAPI ServiceCtrlHandler(DWORD ctrl, DWORD, LPVOID, LPVOID) {
    switch (ctrl) {
        case SERVICE_CONTROL_STOP:
        case SERVICE_CONTROL_SHUTDOWN:
            setState(SERVICE_STOP_PENDING, NO_ERROR, 3000);
            if (g_stopEvent) ::SetEvent(g_stopEvent);
            return NO_ERROR;
        case SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR;
        default:
            return ERROR_CALL_NOT_IMPLEMENTED;
    }
}

void runLoop() {
    // The controller owns the IGCL/Level-Zero handles. It is normally held for the
    // service lifetime, but a controller initialized against a not-yet-ready driver
    // (cold boot) or invalidated by a driver reset keeps failing forever with stale
    // handles. So: RE-INITIALIZE a fresh controller whenever an apply fails, and
    // retry fast until the first success. No Intel service / waiver window is
    // needed -- fan and OC both work with the Intel service disabled once the
    // driver is ready and we hold fresh handles.
    if (!g_applyEvent) g_applyEvent = createApplyEvent();

    auto ctl = std::make_unique<ArcController>();
    std::string err;
    bool inited = ctl->init(err);
    if (!inited) logLine("init failed (will retry): " + err);

    bool everApplied = false;
    for (;;) {
        bool ok = inited && applyOnce(*ctl);
        if (ok) {
            everApplied = true;
        } else {
            // Fresh handles: recover from cold-boot-not-ready or a driver reset.
            logLine("re-initializing controller (apply failed / driver not ready)");
            ctl = std::make_unique<ArcController>();
            inited = ctl->init(err);
            if (!inited) logLine("re-init failed (will retry): " + err);
        }
        // Poll fast until the profile has applied at least once, then relax. Wake
        // early if the GUI signals the apply event after saving a new profile.
        const DWORD interval = everApplied ? kReapplyIntervalMs : kBootRetryMs;
        HANDLE waits[2] = { g_stopEvent, g_applyEvent };
        const DWORD n = g_applyEvent ? 2u : 1u;
        const DWORD w = ::WaitForMultipleObjects(n, waits, FALSE, interval);
        if (w == WAIT_OBJECT_0) break;     // stop requested
        // WAIT_OBJECT_0+1 (apply now) or WAIT_TIMEOUT -> loop and re-apply.
    }
}

void WINAPI ServiceMain(DWORD, LPWSTR*) {
    g_statusHandle = ::RegisterServiceCtrlHandlerExW(kServiceName, ServiceCtrlHandler, nullptr);
    if (!g_statusHandle) return;

    g_status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    setState(SERVICE_START_PENDING, NO_ERROR, 3000);

    g_stopEvent = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!g_stopEvent) { setState(SERVICE_STOPPED, GetLastError()); return; }

    setState(SERVICE_RUNNING);
    logLine("service started");
    runLoop();
    logLine("service stopping");

    ::CloseHandle(g_stopEvent);
    g_stopEvent = nullptr;
    setState(SERVICE_STOPPED);
}

std::wstring exePath() {
    wchar_t buf[MAX_PATH];
    ::GetModuleFileNameW(nullptr, buf, MAX_PATH);
    return buf;
}

int installService() {
    SC_HANDLE scm = ::OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CREATE_SERVICE);
    if (!scm) { std::fprintf(stderr, "OpenSCManager failed (%lu). Run as Administrator.\n", GetLastError()); return 1; }

    // Register with NO argument: launched by the SCM, wmain must fall through to
    // StartServiceCtrlDispatcher (the "run" arg is the foreground DEBUG path and
    // never reports RUNNING to the SCM -> 30s connect timeout / SCM kills it).
    const std::wstring cmd = L"\"" + exePath() + L"\"";
    SC_HANDLE svc = ::CreateServiceW(
        scm, kServiceName, kDisplayName, SERVICE_ALL_ACCESS,
        SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
        cmd.c_str(), nullptr, nullptr, nullptr, nullptr, nullptr);
    int rc = 0;
    if (!svc) {
        const DWORD e = GetLastError();
        if (e == ERROR_SERVICE_EXISTS) {
            // Already registered (maybe with the old buggy bin path): update it.
            SC_HANDLE ex = ::OpenServiceW(scm, kServiceName, SERVICE_CHANGE_CONFIG | SERVICE_START);
            if (ex) {
                ::ChangeServiceConfigW(ex, SERVICE_NO_CHANGE, SERVICE_AUTO_START,
                    SERVICE_NO_CHANGE, cmd.c_str(), nullptr, nullptr, nullptr, nullptr, nullptr, nullptr);
                std::printf("Service already installed; updated config + (re)starting.\n");
                ::StartServiceW(ex, 0, nullptr);
                ::CloseServiceHandle(ex);
            } else {
                std::printf("Service already installed.\n");
            }
        } else { std::fprintf(stderr, "CreateService failed (%lu).\n", e); rc = 1; }
    } else {
        std::printf("Installed '%ls' (auto-start).\n", kDisplayName);
        ::StartServiceW(svc, 0, nullptr);   // start immediately
        ::CloseServiceHandle(svc);
    }
    ::CloseServiceHandle(scm);
    return rc;
}

int uninstallService() {
    SC_HANDLE scm = ::OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT);
    if (!scm) { std::fprintf(stderr, "OpenSCManager failed (%lu).\n", GetLastError()); return 1; }
    SC_HANDLE svc = ::OpenServiceW(scm, kServiceName, SERVICE_STOP | DELETE);
    int rc = 0;
    if (!svc) {
        std::fprintf(stderr, "OpenService failed (%lu). Not installed?\n", GetLastError());
        rc = 1;
    } else {
        SERVICE_STATUS st{};
        ::ControlService(svc, SERVICE_CONTROL_STOP, &st);
        if (::DeleteService(svc)) std::printf("Uninstalled '%ls'.\n", kDisplayName);
        else { std::fprintf(stderr, "DeleteService failed (%lu).\n", GetLastError()); rc = 1; }
        ::CloseServiceHandle(svc);
    }
    ::CloseServiceHandle(scm);
    return rc;
}

} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc >= 2) {
        const std::wstring a = argv[1];
        if (a == L"install")   return installService();
        if (a == L"uninstall") return uninstallService();
        if (a == L"run") {     // foreground debug run
            g_stopEvent = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
            runLoop();
            return 0;
        }
    }

    // No recognised arg => launched by the SCM.
    SERVICE_TABLE_ENTRYW table[] = {
        {const_cast<LPWSTR>(kServiceName), ServiceMain},
        {nullptr, nullptr},
    };
    if (!::StartServiceCtrlDispatcherW(table)) {
        std::fprintf(stderr,
            "Not started by the Service Control Manager.\n"
            "Use: arc-fan-service install | uninstall | run\n");
        return 1;
    }
    return 0;
}
