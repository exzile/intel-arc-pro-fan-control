// fan_curve.cpp — fan-curve parsing/formatting.
#include "fan_curve.hpp"

#include <algorithm>
#include <sstream>

namespace arc {

bool parseFanCurve(const std::string& spec, std::vector<FanPoint>& out, std::string& err) {
    out.clear();
    std::istringstream iss(spec);
    std::string tok;
    while (iss >> tok) {
        const size_t colon = tok.find(':');
        if (colon == std::string::npos) {
            err = "bad fan point '" + tok + "' (expected temp:percent, e.g. 65:70)";
            return false;
        }
        int temp = 0, pct = 0;
        try {
            temp = std::stoi(tok.substr(0, colon));
            pct = std::stoi(tok.substr(colon + 1));
        } catch (...) {
            err = "bad fan point '" + tok + "' (non-numeric)";
            return false;
        }
        if (temp < 0 || temp > 127) {
            err = "temperature out of range in '" + tok + "' (0-127 C)";
            return false;
        }
        if (pct < 0 || pct > 100) {
            err = "percent out of range in '" + tok + "' (0-100)";
            return false;
        }
        out.push_back(FanPoint{temp, pct});
    }
    if (out.empty()) {
        err = "no fan points given";
        return false;
    }
    std::sort(out.begin(), out.end(), [](const FanPoint& a, const FanPoint& b) {
        return a.temperatureC < b.temperatureC;
    });
    return true;
}

std::string formatFanCurve(const std::vector<FanPoint>& pts) {
    std::vector<FanPoint> sorted(pts);
    std::sort(sorted.begin(), sorted.end(), [](const FanPoint& a, const FanPoint& b) {
        return a.temperatureC < b.temperatureC;
    });
    std::string s;
    for (size_t i = 0; i < sorted.size(); ++i) {
        if (i) s += ' ';
        s += std::to_string(sorted[i].temperatureC);
        s += ':';
        s += std::to_string(sorted[i].speedPercent);
    }
    return s;
}

} // namespace arc
