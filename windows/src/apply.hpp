// apply.hpp — apply a persisted AppConfig to the GPU.
//
// Shared by the CLI (`arc-gpu apply`) and the Windows service so both take the
// exact same code path when re-establishing a saved fan curve / overclock.
#pragma once

#include <string>
#include "arc.hpp"
#include "config.hpp"

namespace arc {

// Apply cfg to the controller. Selects cfg.bdf if set. Applies the fan mode and,
// when cfg.ocApply is true, the overclock knobs that are present. Collects any
// per-step failures into `err` but keeps going (best-effort), returning false if
// anything failed so callers can log it.
bool applyProfile(ArcController& arc, const AppConfig& cfg, std::string& err);

} // namespace arc
