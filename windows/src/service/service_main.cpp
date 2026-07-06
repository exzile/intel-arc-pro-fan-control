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
#include <cstdio>
#include <string>

#include "../arc.hpp"
#include "../config.hpp"
#include "../apply.hpp"

using namespace arc;

namespace {

const wchar_t* kServiceName = L"ArcFanControl";
const wchar_t* kDisplayName = L"Arc Fan Control Service";

SERVICE_STATUS        g_status{};
SERVICE_STATUS_HANDLE g_statusHandle = nullptr;
HANDLE                g_stopEvent = nullptr;

// Re-apply cadence. Long enough to be unobtrusive, short enough to restore the
// profile promptly after a driver reset / resume.
constexpr DWORD kReapplyIntervalMs = 60'000;

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

// Load config + apply once. Re-inits the controller each pass so a driver
// reset (which invalidates IGCL handles) is recovered cleanly.
void applyOnce() {
    ArcController a;
    std::string err;
    if (!a.init(err)) { logLine("init failed: " + err); return; }

    AppConfig cfg;
    loadConfig(cfg, err);
    if (cfg.fanMode == FanMode::None && !cfg.ocApply) {
        logLine("no saved profile to apply");
        return;
    }
    std::string applyErr;
    if (applyProfile(a, cfg, applyErr))
        logLine("profile applied");
    else
        logLine("profile applied with warnings: " + applyErr);
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
    applyOnce();   // startup apply
    for (;;) {
        const DWORD w = ::WaitForSingleObject(g_stopEvent, kReapplyIntervalMs);
        if (w == WAIT_OBJECT_0) break;     // stop requested
        applyOnce();                       // periodic re-apply
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

    const std::wstring cmd = L"\"" + exePath() + L"\" run";
    SC_HANDLE svc = ::CreateServiceW(
        scm, kServiceName, kDisplayName, SERVICE_ALL_ACCESS,
        SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
        cmd.c_str(), nullptr, nullptr, nullptr, nullptr, nullptr);
    int rc = 0;
    if (!svc) {
        const DWORD e = GetLastError();
        if (e == ERROR_SERVICE_EXISTS) std::printf("Service already installed.\n");
        else { std::fprintf(stderr, "CreateService failed (%lu).\n", e); rc = 1; }
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
