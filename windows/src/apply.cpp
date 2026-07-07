// apply.cpp — apply a persisted profile to the GPU (fan + overclock).
#include "apply.hpp"

namespace arc {

bool applyProfile(ArcController& arc, const AppConfig& cfg, std::string& err,
                  bool* fanApplied) {
    bool ok = true;
    bool fanOk = true;   // "the fan portion succeeded" (true when there's none)
    std::string step;

    // The caller selects the target adapter (per-GPU profiles are keyed by PCI
    // device id now — BDF is unreliable on Windows). applyProfile acts on the
    // currently-selected adapter.

    // --- Fan ---
    switch (cfg.fanMode) {
        case FanMode::Auto:
            if (!arc.fanSetAuto(step)) { err += "fan auto: " + step + "; "; ok = fanOk = false; }
            break;
        case FanMode::Max:
            if (!arc.fanSetFixed(100, step)) { err += "fan max: " + step + "; "; ok = fanOk = false; }
            break;
        case FanMode::Fixed:
            if (!arc.fanSetFixed(cfg.fixedPercent, step)) { err += "fan fixed: " + step + "; "; ok = fanOk = false; }
            break;
        case FanMode::Curve:
            if (!arc.fanSetCurve(cfg.curve, step)) { err += "fan curve: " + step + "; "; ok = fanOk = false; }
            break;
        case FanMode::None:
            break;   // leave the fan as-is
    }
    if (fanApplied) *fanApplied = fanOk;

    // --- Overclock / tuning ---
    if (cfg.ocApply) {
        if (cfg.hasPowerW && !arc.setPowerLimit(cfg.powerW, step)) { err += "power: " + step + "; "; ok = false; }
        if (cfg.hasTempC  && !arc.setTempLimit(cfg.tempC, step))   { err += "temp: " + step + "; ";  ok = false; }
        if (cfg.hasMemSpeed && !arc.setMemSpeed(cfg.memSpeed, step)){ err += "mem: " + step + "; ";   ok = false; }
        if (cfg.hasFreqOffset && !arc.setGpuFreqOffset(cfg.freqOffset, step)) { err += "freq: " + step + "; "; ok = false; }
        // Voltage offset takes the warranty waiver internally.
        if (cfg.hasVoltOffset && !arc.setGpuVoltageOffset(cfg.voltOffset, step)) { err += "volt: " + step + "; "; ok = false; }
        // Custom voltage-frequency curve (manual mode) — replaces the offset.
        if (!cfg.vfCurve.empty() && !arc.writeVFCurve(cfg.vfCurve, step)) { err += "vfcurve: " + step + "; "; ok = false; }
    }

    return ok;
}

} // namespace arc
