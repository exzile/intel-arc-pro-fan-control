// config.cpp — INI-backed persistence for the fan + overclock profile.
#include "config.hpp"
#include "fan_curve.hpp"

#include <windows.h>
#include <fstream>
#include <sstream>
#include <cstdlib>

namespace arc {
namespace {

std::string trim(const std::string& s) {
    const char* ws = " \t\r\n";
    const size_t a = s.find_first_not_of(ws);
    if (a == std::string::npos) return "";
    const size_t b = s.find_last_not_of(ws);
    return s.substr(a, b - a + 1);
}

std::string fanModeToString(FanMode m) {
    switch (m) {
        case FanMode::Auto:  return "auto";
        case FanMode::Max:   return "max";
        case FanMode::Fixed: return "fixed";
        case FanMode::Curve: return "curve";
        default:             return "none";
    }
}

FanMode fanModeFromString(const std::string& s) {
    if (s == "auto")  return FanMode::Auto;
    if (s == "max")   return FanMode::Max;
    if (s == "fixed") return FanMode::Fixed;
    if (s == "curve") return FanMode::Curve;
    return FanMode::None;
}

} // namespace

std::string configDir() {
    const char* pd = std::getenv("ProgramData");
    std::string base = (pd && *pd) ? pd : "C:\\ProgramData";
    return base + "\\ArcFanControl";
}

std::string configPath() {
    return configDir() + "\\config.ini";
}

bool ensureConfigDir(std::string& err) {
    const std::string dir = configDir();
    if (::CreateDirectoryA(dir.c_str(), nullptr) ||
        ::GetLastError() == ERROR_ALREADY_EXISTS) {
        return true;
    }
    err = "cannot create config directory '" + dir + "'";
    return false;
}

namespace {

// Apply one key=value to a profile. `section` is "fan"/"oc" for the LEGACY
// sectioned format, or "" for the flat per-adapter / named-profile keys
// (fan_mode, fan_curve, oc_apply, oc_freq_offset, ...).
void applyKV(AppConfig& c, const std::string& section,
             const std::string& key, const std::string& val) {
    auto setD  = [&](bool& has, double& dst) { try { dst = std::stod(val); has = true; } catch (...) {} };
    auto asInt = [&](int& dst) { try { dst = std::stoi(val); } catch (...) {} };
    auto asBool = [&]() { return val == "1" || val == "true" || val == "yes"; };

    if (section == "fan") {                                  // legacy [fan]
        if (key == "mode") c.fanMode = fanModeFromString(val);
        else if (key == "fixed") asInt(c.fixedPercent);
        else if (key == "curve") { std::string e; parseFanCurve(val, c.curve, e); }
        return;
    }
    if (section == "oc") {                                   // legacy [oc]
        if (key == "apply") c.ocApply = asBool();
        else if (key == "freq_offset") setD(c.hasFreqOffset, c.freqOffset);
        else if (key == "volt_offset") setD(c.hasVoltOffset, c.voltOffset);
        else if (key == "mem_speed")   setD(c.hasMemSpeed, c.memSpeed);
        else if (key == "power_w")     setD(c.hasPowerW, c.powerW);
        else if (key == "temp_c")      setD(c.hasTempC, c.tempC);
        return;
    }
    // Flat per-adapter / named-profile keys.
    if (key == "fan_mode") c.fanMode = fanModeFromString(val);
    else if (key == "fan_fixed") asInt(c.fixedPercent);
    else if (key == "fan_curve") { std::string e; parseFanCurve(val, c.curve, e); }
    else if (key == "oc_apply") c.ocApply = asBool();
    else if (key == "oc_freq_offset") setD(c.hasFreqOffset, c.freqOffset);
    else if (key == "oc_volt_offset") setD(c.hasVoltOffset, c.voltOffset);
    else if (key == "oc_mem_speed")   setD(c.hasMemSpeed, c.memSpeed);
    else if (key == "oc_power_w")     setD(c.hasPowerW, c.powerW);
    else if (key == "oc_temp_c")      setD(c.hasTempC, c.tempC);
    else if (key == "vf_curve") {
        c.vfCurve.clear();
        std::istringstream ss(val); std::string tok;
        while (ss >> tok) {
            const size_t p = tok.find(':');
            if (p == std::string::npos) continue;
            try {
                VFPoint v;
                v.voltageMv = (uint32_t)std::stoul(tok.substr(0, p));
                v.freqMHz   = (uint32_t)std::stoul(tok.substr(p + 1));
                c.vfCurve.push_back(v);
            } catch (...) {}
        }
    }
}

// Serialize one profile's fields as flat keys (no section header).
void writeProfileBody(std::ostream& o, const AppConfig& c) {
    o << "fan_mode = " << fanModeToString(c.fanMode) << "\n";
    o << "fan_fixed = " << c.fixedPercent << "\n";
    o << "fan_curve = " << formatFanCurve(c.curve) << "\n";
    o << "oc_apply = " << (c.ocApply ? "true" : "false") << "\n";
    if (c.hasFreqOffset) o << "oc_freq_offset = " << c.freqOffset << "\n";
    if (c.hasVoltOffset) o << "oc_volt_offset = " << c.voltOffset << "\n";
    if (c.hasMemSpeed)   o << "oc_mem_speed = "   << c.memSpeed   << "\n";
    if (c.hasPowerW)     o << "oc_power_w = "     << c.powerW     << "\n";
    if (c.hasTempC)      o << "oc_temp_c = "      << c.tempC      << "\n";
    if (!c.vfCurve.empty()) {
        o << "vf_curve = ";
        for (size_t i = 0; i < c.vfCurve.size(); ++i) {
            if (i) o << ' ';
            o << c.vfCurve[i].voltageMv << ':' << c.vfCurve[i].freqMHz;
        }
        o << "\n";
    }
}

// Parse a single-profile file (named profiles) — accepts flat keys + legacy.
void parseSingle(std::istream& f, AppConfig& out) {
    std::string line, section;
    while (std::getline(f, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        if (line.front() == '[' && line.back() == ']') { section = trim(line.substr(1, line.size() - 2)); continue; }
        const size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string sec = (section == "fan" || section == "oc") ? section : "";
        applyKV(out, sec, trim(line.substr(0, eq)), trim(line.substr(eq + 1)));
    }
}

// Parse the multi-adapter config. [adapter.<key>] -> byKey[<key>];
// [adapter.default] or a legacy [fan]/[oc] file -> byKey[""] (the default bucket).
void parseMulti(std::istream& f, MultiConfig& out) {
    std::string line, section;
    while (std::getline(f, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        if (line.front() == '[' && line.back() == ']') { section = trim(line.substr(1, line.size() - 2)); continue; }
        const size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trim(line.substr(0, eq)), val = trim(line.substr(eq + 1));

        std::string adKey, sec;
        if (section.rfind("adapter.", 0) == 0) {
            adKey = section.substr(8);
            if (adKey == "default") adKey = "";
            sec = "";                                  // flat keys
        } else if (section == "fan" || section == "oc") {
            adKey = ""; sec = section;                 // legacy -> default bucket
        } else {
            continue;                                  // legacy [adapter] bdf, unknown
        }
        applyKV(out.byKey[adKey], sec, key, val);
    }
}

bool writeFile(const std::string& path, const std::string& body, std::string& err) {
    std::ofstream f(path, std::ios::trunc);
    if (!f.is_open()) { err = "cannot write file '" + path + "'"; return false; }
    f << body;
    if (!f.good()) { err = "write error on '" + path + "'"; return false; }
    return true;
}

} // namespace

const AppConfig* MultiConfig::find(const std::string& key) const {
    auto it = byKey.find(key);
    if (it != byKey.end()) return &it->second;
    it = byKey.find("");
    if (it != byKey.end()) return &it->second;
    return nullptr;
}

bool loadAllConfigs(MultiConfig& out, std::string& err) {
    out = MultiConfig{};
    std::ifstream f(configPath());
    if (!f.is_open()) return true;   // no file yet => empty
    parseMulti(f, out);
    return true;
}

bool saveAllConfigs(const MultiConfig& cfg, std::string& err) {
    if (!ensureConfigDir(err)) return false;
    std::ostringstream o;
    o << "# Arc Fan Control profiles — one per GPU, re-applied at boot by the service.\n";
    o << "# Sections are keyed by PCI device id: e211 = Arc Pro B60, e223 = B70.\n";
    o << "# [adapter.default] applies to any adapter without its own section.\n\n";
    for (const auto& kv : cfg.byKey) {
        const std::string name = kv.first.empty() ? "default" : kv.first;
        o << "[adapter." << name << "]\n";
        writeProfileBody(o, kv.second);
        o << "\n";
    }
    return writeFile(configPath(), o.str(), err);
}

bool loadConfigFor(const std::string& key, AppConfig& out, std::string& err) {
    MultiConfig m;
    if (!loadAllConfigs(m, err)) return false;
    const AppConfig* p = m.find(key);
    out = p ? *p : AppConfig{};
    return true;
}

bool saveConfigFor(const std::string& key, const AppConfig& cfg, std::string& err) {
    MultiConfig m;
    if (!loadAllConfigs(m, err)) return false;   // preserve other adapters' sections
    m.byKey[key] = cfg;
    return saveAllConfigs(m, err);
}

bool loadConfig(AppConfig& out, std::string& err) {
    return loadConfigFor("", out, err);          // the default profile
}

bool saveConfig(const AppConfig& cfg, std::string& err) {
    return saveConfigFor("", cfg, err);          // write as the default profile
}

std::string profilesDir() {
    return configDir() + "\\profiles";
}

std::string profilePath(const std::string& name) {
    return profilesDir() + "\\" + name + ".ini";
}

std::vector<std::string> listProfiles() {
    std::vector<std::string> names;
    WIN32_FIND_DATAA fd{};
    const std::string glob = profilesDir() + "\\*.ini";
    HANDLE h = ::FindFirstFileA(glob.c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return names;
    do {
        std::string n = fd.cFileName;
        const size_t dot = n.rfind(".ini");
        if (dot != std::string::npos) n = n.substr(0, dot);
        if (!n.empty()) names.push_back(n);
    } while (::FindNextFileA(h, &fd));
    ::FindClose(h);
    return names;
}

bool saveNamedProfile(const std::string& name, const AppConfig& cfg, std::string& err) {
    if (name.empty() || name.find_first_of("\\/:*?\"<>|") != std::string::npos) {
        err = "invalid profile name '" + name + "'";
        return false;
    }
    const std::string dir = profilesDir();
    if (!::CreateDirectoryA(configDir().c_str(), nullptr) &&
        ::GetLastError() != ERROR_ALREADY_EXISTS) {
        err = "cannot create config directory '" + configDir() + "'";
        return false;
    }
    if (!::CreateDirectoryA(dir.c_str(), nullptr) &&
        ::GetLastError() != ERROR_ALREADY_EXISTS) {
        err = "cannot create profiles directory '" + dir + "'";
        return false;
    }
    std::ostringstream body;
    body << "# Arc named overclock/fan profile.\n\n";
    writeProfileBody(body, cfg);
    return writeFile(profilePath(name), body.str(), err);
}

bool loadNamedProfile(const std::string& name, AppConfig& out, std::string& err) {
    out = AppConfig{};
    std::ifstream f(profilePath(name));
    if (!f.is_open()) { err = "profile '" + name + "' not found"; return false; }
    parseSingle(f, out);
    return true;
}

bool deleteProfile(const std::string& name, std::string& err) {
    if (::DeleteFileA(profilePath(name).c_str())) return true;
    err = "cannot delete profile '" + name + "'";
    return false;
}

} // namespace arc
