// arc-gpu — command-line control for Intel Arc on Windows (IGCL backend).
//
// Windows analogue of the Linux xe-gpu / xe-fan-curve / xe-gpu-oc helpers.
//
//   arc-gpu [--bdf BDF] <command> ...
//
//   status                       one-shot dashboard (freq/power/temp/fan/util)
//   list                         list Intel adapters
//   fan show                     fan properties + current state + active curve
//   fan set T:P T:P ...          set + persist a fan curve (temp:percent)
//   fan auto | fan max           stock auto table / full speed
//   fan fixed P                  hold a fixed percent
//   tune show                    power/temp/offset state
//   tune power W                 set + persist sustained power limit (Watts)
//   oc read [stock]              print the live (or stock) VF curve
//   oc freq MHZ                  GPU frequency offset
//   oc volt MV                   GPU voltage offset  (sets warranty waiver)
//   oc mem VAL                   VRAM memory-speed limit (IGCL units)
//   oc temp C                    GPU temperature (throttle) limit
//   oc vfcurve V:F V:F ...       write a custom VF curve (sets warranty waiver)
//   oc reset                     reset all overclock knobs to stock
//   oc profile save|load|list|delete [name]   named overclock profiles
//   temps                        per-sensor temperatures (GPU/VRAM/global)
//   apply                        re-apply the saved profile (used by the service)
//
// Writes and profile saves need Administrator + a supported Intel Arc driver.
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "../arc.hpp"
#include "../config.hpp"
#include "../fan_curve.hpp"
#include "../apply.hpp"

using namespace arc;

namespace {

void usage() {
    std::printf(
        "arc-gpu — Intel Arc control (Windows/IGCL)\n\n"
        "Usage: arc-gpu [--bdf BDF] <command> [args]\n\n"
        "  status                     one-shot dashboard\n"
        "  list                       list Intel adapters\n"
        "  fan show                   fan properties + current state\n"
        "  fan set T:P T:P ...        set + persist a fan curve (temp:percent)\n"
        "  fan auto | max | fixed P   stock table / full speed / fixed percent\n"
        "  tune show                  power/temp/offset state\n"
        "  tune power W               set + persist sustained power limit (W)\n"
        "  oc read [stock]            print live (or stock) VF curve\n"
        "  oc freq MHZ                GPU frequency offset\n"
        "  oc volt MV                 GPU voltage offset (warranty waiver)\n"
        "  oc mem VAL                 VRAM memory-speed limit\n"
        "  oc temp C                  GPU temperature limit\n"
        "  oc vfcurve V:F V:F ...      write a custom VF curve (warranty waiver)\n"
        "  oc reset                   reset overclock to stock\n"
        "  oc profile save|load|list|delete [name]   named OC profiles\n"
        "  temps                      per-sensor temperatures\n"
        "  apply                      re-apply the saved profile\n");
}

int fail(const std::string& msg) {
    std::fprintf(stderr, "arc-gpu: %s\n", msg.c_str());
    return 1;
}

// Per-adapter profile key for the currently-selected GPU (PCI device id hex, e.g.
// "e211" = B60, "e223" = B70). Profiles are stored/applied per adapter.
std::string curKey(ArcController& a) {
    const AdapterInfo* d = a.current();
    return d ? d->key() : std::string();
}

// Two telemetry samples an interval apart give instantaneous rate metrics.
bool sampleMetrics(ArcController& a, Metrics& out, std::string& err) {
    Telemetry t0, t1;
    if (!a.sampleTelemetry(t0, err)) return false;
    ::Sleep(300);
    if (!a.sampleTelemetry(t1, err)) return false;
    out = ArcController::deriveMetrics(t0, t1);
    return true;
}

int cmdList(ArcController& a) {
    for (size_t i = 0; i < a.adapters().size(); ++i) {
        const AdapterInfo& d = a.adapters()[i];
        std::printf("[%zu] %-40s  %04x:%04x  %s%s\n", i, d.name.c_str(),
                    d.pciVendorId, d.pciDeviceId, d.bdfString().c_str(),
                    (a.current() == &d) ? "  (selected)" : "");
    }
    return 0;
}

int cmdStatus(ArcController& a) {
    const AdapterInfo* d = a.current();
    if (!d) return fail("no adapter selected");
    std::printf("Device : %s  (%04x:%04x  %s)\n", d->name.c_str(),
                d->pciVendorId, d->pciDeviceId, d->bdfString().c_str());

    std::string err;
    Metrics m;
    if (sampleMetrics(a, m, err)) {
        std::printf("Clock  : %.0f MHz\n", m.gpuFreqMHz);
        if (m.hasCardPower) std::printf("Power  : %.1f W (card)", m.cardPowerW);
        if (m.hasGpuPower)  std::printf("   %.1f W (gpu)", m.gpuPowerW);
        if (m.hasCardPower || m.hasGpuPower) std::printf("\n");
        std::printf("Temp   : GPU %.0f C", m.gpuTempC);
        if (m.vramTempC > 0) std::printf("   VRAM %.0f C", m.vramTempC);
        std::printf("\n");
        if (m.hasGpuUtil) std::printf("Util   : %.0f%% GPU", m.gpuUtilPct);
        if (m.hasRenderUtil) std::printf("   %.0f%% render", m.renderUtilPct);
        if (m.hasMediaUtil)  std::printf("   %.0f%% media", m.mediaUtilPct);
        if (m.hasGpuUtil || m.hasRenderUtil || m.hasMediaUtil) std::printf("\n");
        std::printf("Limited: %s%s%s%s\n",
                    m.powerLimited ? "power " : "",
                    m.tempLimited ? "temp " : "",
                    m.voltageLimited ? "voltage " : "",
                    (!m.powerLimited && !m.tempLimited && !m.voltageLimited) ? "no" : "");
    } else {
        std::fprintf(stderr, "  (telemetry unavailable: %s)\n", err.c_str());
    }

    int rpm = -1, pct = -1;
    std::string fe;
    if (a.fanGetRpm(rpm, fe) && a.fanGetPercent(pct, fe))
        std::printf("Fan    : %d RPM (%d%%)\n", rpm, pct);

    MemoryInfo mem;
    if (a.readMemory(mem, fe) && mem.totalBytes > 0) {
        const double gib = 1024.0 * 1024.0 * 1024.0;
        std::printf("VRAM   : %.1f / %.1f GiB (%.0f%%)\n",
                    mem.usedBytes / gib, mem.totalBytes / gib,
                    100.0 * mem.usedBytes / mem.totalBytes);
    }
    return 0;
}

int cmdFan(ArcController& a, const std::vector<std::string>& args) {
    if (args.empty()) { usage(); return 1; }
    const std::string& sub = args[0];
    std::string err;

    if (sub == "show") {
        FanProperties p;
        if (!a.fanProperties(p, err)) return fail(err);
        std::printf("Controllable : %s\n", p.canControl ? "yes" : "no");
        std::printf("Max RPM      : %d\n", p.maxRPM);
        std::printf("Table points : %d\n", p.maxPoints);
        std::printf("Modes        : %s%s\n", p.supportsFixed ? "fixed " : "",
                    p.supportsTable ? "table" : "");
        int rpm = -1, pct = -1;
        if (a.fanGetRpm(rpm, err)) std::printf("Current      : %d RPM", rpm);
        if (a.fanGetPercent(pct, err)) std::printf(" (%d%%)", pct);
        std::printf("\n");
        std::vector<FanPoint> curve;
        if (a.fanGetCurve(curve, err) && !curve.empty())
            std::printf("Active curve : %s\n", formatFanCurve(curve).c_str());
        return 0;
    }

    // Mutating subcommands: apply now + persist to THIS adapter's profile.
    AppConfig cfg;
    loadConfigFor(curKey(a), cfg, err);

    if (sub == "auto") {
        if (!a.fanSetAuto(err)) return fail(err);
        cfg.fanMode = FanMode::Auto;
    } else if (sub == "max") {
        if (!a.fanSetFixed(100, err)) return fail(err);
        cfg.fanMode = FanMode::Max;
    } else if (sub == "fixed") {
        if (args.size() < 2) return fail("fan fixed needs a percent");
        int pct = std::atoi(args[1].c_str());
        if (!a.fanSetFixed(pct, err)) return fail(err);
        cfg.fanMode = FanMode::Fixed;
        cfg.fixedPercent = pct;
    } else if (sub == "set") {
        std::string spec;
        for (size_t i = 1; i < args.size(); ++i) { if (i > 1) spec += ' '; spec += args[i]; }
        std::vector<FanPoint> pts;
        if (!parseFanCurve(spec, pts, err)) return fail(err);
        if (!a.fanSetCurve(pts, err)) return fail(err);
        cfg.fanMode = FanMode::Curve;
        cfg.curve = pts;
    } else {
        return fail("unknown fan subcommand '" + sub + "'");
    }

    if (!saveConfigFor(curKey(a), cfg, err))
        std::fprintf(stderr, "arc-gpu: applied, but could not save profile: %s\n", err.c_str());
    else
        std::printf("OK (saved to %s)\n", configPath().c_str());
    return 0;
}

int cmdTune(ArcController& a, const std::vector<std::string>& args) {
    if (args.empty()) { usage(); return 1; }
    std::string err;
    if (args[0] == "show") {
        OcState s;
        if (!a.ocGetState(s, err)) return fail(err);
        std::printf("OC supported : %s\n", s.supported ? "yes" : "no");
        if (s.hasPowerLimit)   std::printf("Power limit  : %.1f W\n", s.powerLimitW);
        if (s.hasTempLimit)    std::printf("Temp limit   : %.1f C\n", s.tempLimitC);
        if (s.hasGpuFreqOffset) std::printf("Freq offset  : %.1f\n", s.gpuFreqOffset);
        if (s.hasGpuVoltOffset) std::printf("Volt offset  : %.1f\n", s.gpuVoltOffset);
        if (s.hasMemSpeed)     std::printf("Mem speed    : %.1f\n", s.memSpeed);
        return 0;
    }
    if (args[0] == "power") {
        if (args.size() < 2) return fail("tune power needs Watts");
        double w = std::atof(args[1].c_str());
        if (!a.setPowerLimit(w, err)) return fail(err);
        AppConfig cfg; loadConfigFor(curKey(a), cfg, err);
        cfg.ocApply = true; cfg.hasPowerW = true; cfg.powerW = w;
        saveConfigFor(curKey(a), cfg, err);
        std::printf("OK\n");
        return 0;
    }
    return fail("unknown tune subcommand '" + args[0] + "'");
}

int cmdTemps(ArcController& a) {
    std::vector<TempSensor> ts;
    std::string err;
    if (!a.readTemperatures(ts, err)) return fail(err);
    if (ts.empty()) { std::printf("No temperature sensors reported.\n"); return 0; }
    for (const TempSensor& s : ts) {
        if (s.currentC < 0)
            std::printf("  %-12s     --    (max %.0f C)\n", s.label.c_str(), s.maxC);
        else
            std::printf("  %-12s %6.1f C  (max %.0f C)\n", s.label.c_str(), s.currentC, s.maxC);
    }
    return 0;
}

// `arc-gpu oc profile <save|load|list|delete> [name]`
int cmdOcProfile(ArcController& a, const std::vector<std::string>& args) {
    std::string err;
    const std::string action = args.empty() ? "list" : args[0];
    const std::string name = args.size() > 1 ? args[1] : "";

    if (action == "list") {
        std::vector<std::string> names = listProfiles();
        if (names.empty()) { std::printf("No saved profiles.\n"); return 0; }
        for (const std::string& n : names) std::printf("  %s\n", n.c_str());
        return 0;
    }
    if (name.empty()) return fail("oc profile " + action + " needs a name");

    if (action == "save") {
        OcState s;
        if (!a.ocGetState(s, err)) return fail(err);
        AppConfig cfg;
        cfg.ocApply = true;
        cfg.hasFreqOffset = s.hasGpuFreqOffset; cfg.freqOffset = s.gpuFreqOffset;
        cfg.hasVoltOffset = s.hasGpuVoltOffset; cfg.voltOffset = s.gpuVoltOffset;
        cfg.hasMemSpeed   = s.hasMemSpeed;      cfg.memSpeed   = s.memSpeed;
        cfg.hasPowerW     = s.hasPowerLimit;    cfg.powerW     = s.powerLimitW;
        cfg.hasTempC      = s.hasTempLimit;     cfg.tempC      = s.tempLimitC;
        if (!saveNamedProfile(name, cfg, err)) return fail(err);
        std::printf("Saved profile '%s'.\n", name.c_str());
        return 0;
    }
    if (action == "load") {
        AppConfig named;
        if (!loadNamedProfile(name, named, err)) return fail(err);
        std::string ae;
        if (!applyProfile(a, named, ae))
            std::fprintf(stderr, "arc-gpu: profile applied with warnings: %s\n", ae.c_str());
        // Merge the OC subset into THIS adapter's profile so the service re-applies it.
        AppConfig active;
        loadConfigFor(curKey(a), active, err);
        active.ocApply = named.ocApply;
        active.hasFreqOffset = named.hasFreqOffset; active.freqOffset = named.freqOffset;
        active.hasVoltOffset = named.hasVoltOffset; active.voltOffset = named.voltOffset;
        active.hasMemSpeed   = named.hasMemSpeed;   active.memSpeed   = named.memSpeed;
        active.hasPowerW     = named.hasPowerW;     active.powerW     = named.powerW;
        active.hasTempC      = named.hasTempC;      active.tempC      = named.tempC;
        saveConfigFor(curKey(a), active, err);
        std::printf("Loaded profile '%s'.\n", name.c_str());
        return 0;
    }
    if (action == "delete") {
        if (!deleteProfile(name, err)) return fail(err);
        std::printf("Deleted profile '%s'.\n", name.c_str());
        return 0;
    }
    return fail("unknown oc profile action '" + action + "'");
}

int cmdOc(ArcController& a, const std::vector<std::string>& args) {
    if (args.empty()) { usage(); return 1; }
    const std::string& sub = args[0];
    std::string err;

    if (sub == "profile")
        return cmdOcProfile(a, std::vector<std::string>(args.begin() + 1, args.end()));

    if (sub == "read") {
        const bool live = !(args.size() > 1 && args[1] == "stock");
        std::vector<VFPoint> curve;
        if (!a.readVFCurve(curve, live, err)) return fail(err);
        std::printf("%s VF curve (%zu points):\n", live ? "Live" : "Stock", curve.size());
        for (size_t i = 0; i < curve.size(); ++i)
            std::printf("  [%2zu] %4u mV  %5u MHz\n", i, curve[i].voltageMv, curve[i].freqMHz);
        return 0;
    }
    if (sub == "reset") {
        if (!a.ocReset(err)) return fail(err);
        std::printf("Overclock reset to stock.\n");
        return 0;
    }

    // Persisted setters (this adapter's profile).
    AppConfig cfg; loadConfigFor(curKey(a), cfg, err);
    cfg.ocApply = true;

    if (sub == "freq") {
        if (args.size() < 2) return fail("oc freq needs a value");
        double v = std::atof(args[1].c_str());
        if (!a.setGpuFreqOffset(v, err)) return fail(err);
        cfg.hasFreqOffset = true; cfg.freqOffset = v;
    } else if (sub == "volt") {
        if (args.size() < 2) return fail("oc volt needs a value");
        double v = std::atof(args[1].c_str());
        if (!a.setGpuVoltageOffset(v, err)) return fail(err);
        cfg.hasVoltOffset = true; cfg.voltOffset = v;
    } else if (sub == "mem") {
        if (args.size() < 2) return fail("oc mem needs a value");
        double v = std::atof(args[1].c_str());
        if (!a.setMemSpeed(v, err)) return fail(err);
        cfg.hasMemSpeed = true; cfg.memSpeed = v;
    } else if (sub == "temp") {
        if (args.size() < 2) return fail("oc temp needs Celsius");
        double v = std::atof(args[1].c_str());
        if (!a.setTempLimit(v, err)) return fail(err);
        cfg.hasTempC = true; cfg.tempC = v;
    } else if (sub == "vfcurve") {
        std::vector<VFPoint> pts;
        for (size_t i = 1; i < args.size(); ++i) {
            const std::string& t = args[i];
            const size_t c = t.find(':');
            if (c == std::string::npos) return fail("bad VF point '" + t + "' (expected mV:MHz)");
            pts.push_back({static_cast<uint32_t>(std::atoi(t.substr(0, c).c_str())),
                           static_cast<uint32_t>(std::atoi(t.substr(c + 1).c_str()))});
        }
        if (!a.writeVFCurve(pts, err)) return fail(err);
        std::printf("Custom VF curve written (%zu points).\n", pts.size());
        return 0;   // VF curve not stored in the simple profile
    } else {
        return fail("unknown oc subcommand '" + sub + "'");
    }

    saveConfigFor(curKey(a), cfg, err);
    std::printf("OK\n");
    return 0;
}

int cmdApply(ArcController& a) {
    AppConfig cfg;
    std::string err;
    loadConfigFor(curKey(a), cfg, err);
    if (!applyProfile(a, cfg, err))
        std::fprintf(stderr, "arc-gpu: profile applied with warnings: %s\n", err.c_str());
    else
        std::printf("Profile applied.\n");
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    std::vector<std::string> args(argv + 1, argv + argc);

    // Global adapter selectors may precede the command. --gpu takes an index
    // (0,1,...) or a device-id key (e211/e223) and is the reliable multi-GPU
    // selector; --bdf is kept for compat but IGCL reports 00:00.0 for all cards.
    std::string bdf, gpuSel;
    while (!args.empty() && (args[0] == "--bdf" || args[0] == "-b" ||
                             args[0] == "--gpu" || args[0] == "-g")) {
        if (args.size() < 2) { usage(); return 1; }
        if (args[0] == "--gpu" || args[0] == "-g") gpuSel = args[1];
        else bdf = args[1];
        args.erase(args.begin(), args.begin() + 2);
    }
    if (args.empty()) { usage(); return 1; }

    const std::string cmd = args[0];
    const std::vector<std::string> rest(args.begin() + 1, args.end());

    if (cmd == "help" || cmd == "--help" || cmd == "-h") { usage(); return 0; }

    ArcController a;
    std::string err;
    if (!a.init(err)) return fail(err);
    if (!bdf.empty() && !a.selectByBdf(bdf, err)) return fail(err);
    if (!gpuSel.empty()) {
        const bool numeric = gpuSel.find_first_not_of("0123456789") == std::string::npos;
        if (numeric) { if (!a.selectByIndex(static_cast<size_t>(std::stoul(gpuSel)), err)) return fail(err); }
        else if (!a.selectByKey(gpuSel, err)) return fail(err);
    }

    if (cmd == "list")   return cmdList(a);
    if (cmd == "status") return cmdStatus(a);
    if (cmd == "fan")    return cmdFan(a, rest);
    if (cmd == "tune")   return cmdTune(a, rest);
    if (cmd == "oc")     return cmdOc(a, rest);
    if (cmd == "temps")  return cmdTemps(a);
    if (cmd == "apply")  return cmdApply(a);

    usage();
    return 1;
}
