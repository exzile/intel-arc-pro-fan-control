// apply.hpp — apply a persisted AppConfig to the GPU.
//
// Shared by the CLI (`arc-gpu apply`) and the Windows service so both take the
// exact same code path when re-establishing a saved fan curve / overclock.
#pragma once

#include <string>
#include "arc.hpp"
#include "config.hpp"

namespace arc {

// Apply cfg to the currently-selected adapter. Applies the fan mode and, when
// cfg.ocApply is true, the overclock knobs that are present. Collects any per-step
// failures into `err` but keeps going (best-effort), returning false if anything
// failed so callers can log it.
//
// If `fanApplied` is non-null it receives whether the FAN portion succeeded (or
// there was no fan to apply). The service uses this to decide whether to retry
// (a failed fan = driver not ready) vs. tolerate an expected OC failure (e.g. the
// firmware-gated B70 overclock), which must not cause an endless re-init loop.
bool applyProfile(ArcController& arc, const AppConfig& cfg, std::string& err,
                  bool* fanApplied = nullptr);

} // namespace arc
