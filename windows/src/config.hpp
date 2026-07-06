// config.hpp — persisted fan + overclock profile shared by the CLI and service.
//
// This is the Windows analogue of /etc/xe-fan-curve.conf + /etc/xe-gpu-oc.conf.
// The card resets to stock on cold boot / driver reset, so the service re-applies
// this profile at startup (mirroring the systemd boot services on Linux).
//
// Stored as a small INI at %ProgramData%\ArcFanControl\config.ini.
#pragma once

#include <map>
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
// only on an unreadable/corrupt file. (Legacy single-profile accessor: returns
// the "default" profile — the one that applies to any adapter without its own.)
bool loadConfig(AppConfig& out, std::string& err);

// Persist config as the "default" profile (creates the directory as needed).
bool saveConfig(const AppConfig& cfg, std::string& err);

// --- Per-adapter profiles (multi-GPU) ---------------------------------------
// Each GPU has its own fan/OC profile keyed by AdapterInfo::key() (PCI device id,
// e.g. "e211" = B60, "e223" = B70). The empty key "" is the DEFAULT profile that
// applies to any adapter without its own entry (and is where a legacy single
// profile migrates). Stored as [adapter.<key>] sections; "" is [adapter.default].
struct MultiConfig {
    std::map<std::string, AppConfig> byKey;
    // Effective profile for an adapter key: its own entry, else the default (""),
    // else nullptr.
    const AppConfig* find(const std::string& key) const;
};

bool loadAllConfigs(MultiConfig& out, std::string& err);
bool saveAllConfigs(const MultiConfig& cfg, std::string& err);
// Load/save one adapter's profile, preserving every other adapter's section.
bool loadConfigFor(const std::string& key, AppConfig& out, std::string& err);
bool saveConfigFor(const std::string& key, const AppConfig& cfg, std::string& err);

// --- Named overclock profiles (mirrors `xe-gpu-oc profile save/load/list`) ---
// Stored as individual INI files under configDir()\profiles\<name>.ini.

// configDir() + "\profiles".
std::string profilesDir();
// profilesDir() + "\<name>.ini".
std::string profilePath(const std::string& name);
// Names (without extension) of all saved profiles.
std::vector<std::string> listProfiles();
// Save/load/delete a named profile. loadNamedProfile fails if it doesn't exist.
bool saveNamedProfile(const std::string& name, const AppConfig& cfg, std::string& err);
bool loadNamedProfile(const std::string& name, AppConfig& out, std::string& err);
bool deleteProfile(const std::string& name, std::string& err);

} // namespace arc
