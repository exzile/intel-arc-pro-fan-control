// config.hpp — persisted fan + overclock profile shared by the CLI and service.
//
// This is the Windows analogue of /etc/xe-fan-curve.conf + /etc/xe-gpu-oc.conf.
// The card resets to stock on cold boot / driver reset, so the service re-applies
// this profile at startup (mirroring the systemd boot services on Linux).
//
// Stored as a small INI at %ProgramData%\ArcFanControl\config.ini.
#pragma once

#include <string>
#include <vector>
#include "arc.hpp"

namespace arc {

enum class FanMode { None, Auto, Max, Fixed, Curve };

struct AppConfig {
    // --- Fan ---
    FanMode fanMode = FanMode::None;   // None => leave fan untouched
    std::vector<FanPoint> curve;       // used when fanMode == Curve
    int fixedPercent = 100;            // used when fanMode == Fixed

    // --- Overclock / tuning (applied only when ocApply is true) ---
    bool ocApply = false;
    bool hasFreqOffset = false; double freqOffset = 0;   // MHz (or IGCL units)
    bool hasVoltOffset = false; double voltOffset = 0;   // mV  (or IGCL units)
    bool hasMemSpeed   = false; double memSpeed   = 0;   // GT/s or Gbps (IGCL units)
    bool hasPowerW     = false; double powerW     = 0;   // Watts
    bool hasTempC      = false; double tempC      = 0;   // Celsius

    // Optional adapter target (short BDF, e.g. "03:00.0"). Empty => default /
    // ARC_GPU_BDF. Lets the service pin a specific card on multi-GPU boxes.
    std::string bdf;
};

// %ProgramData%\ArcFanControl (no trailing slash).
std::string configDir();
// configDir() + "\config.ini".
std::string configPath();

// Create configDir() if missing. Returns false with err on failure.
bool ensureConfigDir(std::string& err);

// Load config. A missing file yields defaults and returns true. Returns false
// only on an unreadable/corrupt file.
bool loadConfig(AppConfig& out, std::string& err);

// Persist config (creates the directory as needed).
bool saveConfig(const AppConfig& cfg, std::string& err);

} // namespace arc
