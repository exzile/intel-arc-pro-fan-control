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

void parseInto(std::istream& f, AppConfig& out) {
    std::string line, section;
    while (std::getline(f, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        if (line.front() == '[' && line.back() == ']') {
            section = trim(line.substr(1, line.size() - 2));
            continue;
        }
        const size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trim(line.substr(0, eq));
        const std::string val = trim(line.substr(eq + 1));

        if (section == "fan") {
            if (key == "mode") out.fanMode = fanModeFromString(val);
            else if (key == "fixed") { try { out.fixedPercent = std::stoi(val); } catch (...) {} }
            else if (key == "curve") { std::string e; parseFanCurve(val, out.curve, e); }
        } else if (section == "oc") {
            auto setD = [&](bool& has, double& dst) {
                try { dst = std::stod(val); has = true; } catch (...) {}
            };
            if (key == "apply") out.ocApply = (val == "1" || val == "true" || val == "yes");
            else if (key == "freq_offset") setD(out.hasFreqOffset, out.freqOffset);
            else if (key == "volt_offset") setD(out.hasVoltOffset, out.voltOffset);
            else if (key == "mem_speed")   setD(out.hasMemSpeed, out.memSpeed);
            else if (key == "power_w")     setD(out.hasPowerW, out.powerW);
            else if (key == "temp_c")      setD(out.hasTempC, out.tempC);
        } else if (section == "adapter") {
            if (key == "bdf") out.bdf = val;
        }
    }
}

std::string serialize(const AppConfig& cfg) {
    std::ostringstream o;
    o << "# Arc Fan Control profile — re-applied at boot by the ArcFanControl service.\n";
    o << "# Managed by arc-gpu; hand-edits are preserved on the next save.\n\n";

    o << "[adapter]\n";
    o << "bdf = " << cfg.bdf << "\n\n";

    o << "[fan]\n";
    o << "mode = " << fanModeToString(cfg.fanMode) << "\n";
    o << "fixed = " << cfg.fixedPercent << "\n";
    o << "curve = " << formatFanCurve(cfg.curve) << "\n\n";

    o << "[oc]\n";
    o << "apply = " << (cfg.ocApply ? "true" : "false") << "\n";
    if (cfg.hasFreqOffset) o << "freq_offset = " << cfg.freqOffset << "\n";
    if (cfg.hasVoltOffset) o << "volt_offset = " << cfg.voltOffset << "\n";
    if (cfg.hasMemSpeed)   o << "mem_speed = "   << cfg.memSpeed   << "\n";
    if (cfg.hasPowerW)     o << "power_w = "     << cfg.powerW     << "\n";
    if (cfg.hasTempC)      o << "temp_c = "      << cfg.tempC      << "\n";
    return o.str();
}

bool writeFile(const std::string& path, const std::string& body, std::string& err) {
    std::ofstream f(path, std::ios::trunc);
    if (!f.is_open()) { err = "cannot write file '" + path + "'"; return false; }
    f << body;
    if (!f.good()) { err = "write error on '" + path + "'"; return false; }
    return true;
}

} // namespace

bool loadConfig(AppConfig& out, std::string& err) {
    out = AppConfig{};
    std::ifstream f(configPath());
    if (!f.is_open()) return true;   // no file yet => defaults
    parseInto(f, out);
    return true;
}

bool saveConfig(const AppConfig& cfg, std::string& err) {
    if (!ensureConfigDir(err)) return false;
    return writeFile(configPath(), serialize(cfg), err);
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
    return writeFile(profilePath(name), serialize(cfg), err);
}

bool loadNamedProfile(const std::string& name, AppConfig& out, std::string& err) {
    out = AppConfig{};
    std::ifstream f(profilePath(name));
    if (!f.is_open()) { err = "profile '" + name + "' not found"; return false; }
    parseInto(f, out);
    return true;
}

bool deleteProfile(const std::string& name, std::string& err) {
    if (::DeleteFileA(profilePath(name).c_str())) return true;
    err = "cannot delete profile '" + name + "'";
    return false;
}

} // namespace arc
