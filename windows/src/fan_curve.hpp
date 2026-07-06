// fan_curve.hpp — parse/format fan curves in the CLI's "temp:percent" notation.
//
// Mirrors the Linux `xe-fan-curve set 45:80 55:130 ...` syntax, except the
// second field is a PERCENT (0-100) rather than a PWM byte (0-255), because the
// IGCL fan table is expressed in percent. A helper is provided to convert a
// legacy PWM value to percent so existing Linux curves can be reused.
#pragma once

#include <string>
#include <vector>
#include "arc.hpp"

namespace arc {

// Convert a Linux-style PWM byte (0-255) to the percent this port uses.
inline int pwmToPercent(int pwm) {
    if (pwm < 0) pwm = 0;
    if (pwm > 255) pwm = 255;
    return (pwm * 100 + 127) / 255;   // rounded
}

// Parse "45:80 55:100 ..." (whitespace-separated temp:percent pairs). Points may
// be given in any order; the caller/HW sorts by temperature. Returns false with
// `err` set on malformed input or out-of-range values.
bool parseFanCurve(const std::string& spec, std::vector<FanPoint>& out, std::string& err);

// Render points back to "temp:percent temp:percent ..." (sorted by temperature).
std::string formatFanCurve(const std::vector<FanPoint>& pts);

} // namespace arc
