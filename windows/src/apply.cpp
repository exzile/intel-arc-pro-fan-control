// apply.cpp — apply a persisted profile to the GPU (fan + overclock).
#include "apply.hpp"

namespace arc {

bool applyProfile(ArcController& arc, const AppConfig& cfg, std::string& err) {
    bool ok = true;
    std::string step;

    if (!cfg.bdf.empty()) {
        if (!arc.selectByBdf(cfg.bdf, step)) {
            err += "adapter: " + step + "; ";
            ok = false;   // continue with the default adapter
        }
    }

    // --- Fan ---
    switch (cfg.fanMode) {
        case FanMode::Auto:
            if (!arc.fanSetAuto(step)) { err += "fan auto: " + step + "; "; ok = false; }
            break;
        case FanMode::Max:
            if (!arc.fanSetFixed(100, step)) { err += "fan max: " + step + "; "; ok = false; }
            break;
        case FanMode::Fixed:
            if (!arc.fanSetFixed(cfg.fixedPercent, step)) { err += "fan fixed: " + step + "; "; ok = false; }
            break;
        case FanMode::Curve:
            if (!arc.fanSetCurve(cfg.curve, step)) { err += "fan curve: " + step + "; "; ok = false; }
            break;
        case FanMode::None:
            break;   // leave the fan as-is
    }

    // --- Overclock / tuning ---
    if (cfg.ocApply) {
        if (cfg.hasPowerW && !arc.setPowerLimit(cfg.powerW, step)) { err += "power: " + step + "; "; ok = false; }
        if (cfg.hasTempC  && !arc.setTempLimit(cfg.tempC, step))   { err += "temp: " + step + "; ";  ok = false; }
        if (cfg.hasMemSpeed && !arc.setMemSpeed(cfg.memSpeed, step)){ err += "mem: " + step + "; ";   ok = false; }
        if (cfg.hasFreqOffset && !arc.setGpuFreqOffset(cfg.freqOffset, step)) { err += "freq: " + step + "; "; ok = false; }
        // Voltage offset takes the warranty waiver internally.
        if (cfg.hasVoltOffset && !arc.setGpuVoltageOffset(cfg.voltOffset, step)) { err += "volt: " + step + "; "; ok = false; }
    }

    return ok;
}

} // namespace arc
