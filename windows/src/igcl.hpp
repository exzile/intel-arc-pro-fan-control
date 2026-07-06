// igcl.hpp — dynamic loader for Intel Graphics Control Library (IGCL / "ctlApi").
//
// The IGCL entry points live in ControlLib.dll, installed by the Intel graphics
// driver. Rather than link against an import library, we LoadLibrary the DLL at
// runtime and resolve the handful of ctl* functions this port needs. This keeps
// the build self-contained (only the public igcl_api.h header is required) and
// lets the tools fail gracefully with a clear message when the Intel driver /
// ControlLib.dll is not present.
//
// The header (third_party/igcl/igcl_api.h) already declares the ctl_pfn<Name>_t
// function-pointer typedefs we bind to, so this file stays in lock-step with
// whatever IGCL revision the user drops in.
//
// Not affiliated with or endorsed by Intel. IGCL is Intel's own library; obtain
// igcl_api.h from https://github.com/intel/drivers.gpu.control-library.
#pragma once

#include <windows.h>
#include <string>
#include "igcl_api.h"

namespace arc {

// Required functions: init/enumeration + the full fan + telemetry surface. If
// any of these is absent the library is unusable and load() fails.
#define ARC_IGCL_REQUIRED(X)                                                    \
    X(ctlInit)                                                                  \
    X(ctlClose)                                                                 \
    X(ctlEnumerateDevices)                                                      \
    X(ctlGetDeviceProperties)                                                   \
    X(ctlEnumFans)                                                              \
    X(ctlFanGetProperties)                                                      \
    X(ctlFanGetConfig)                                                          \
    X(ctlFanSetDefaultMode)                                                     \
    X(ctlFanSetFixedSpeedMode)                                                  \
    X(ctlFanSetSpeedTableMode)                                                  \
    X(ctlFanGetState)                                                           \
    X(ctlPowerTelemetryGet)

// Optional functions: the overclock/tuning surface and per-sensor temperatures.
// Older ControlLib.dll builds may lack the V2 overclock API — we resolve what is
// present and leave the rest null so fan control still works. Callers must
// null-check before use (arc.cpp does).
#define ARC_IGCL_OPTIONAL(X)                                                    \
    X(ctlEnumTemperatureSensors)                                                \
    X(ctlTemperatureGetProperties)                                              \
    X(ctlTemperatureGetState)                                                   \
    X(ctlOverclockGetProperties)                                               \
    X(ctlOverclockWaiverSet)                                                    \
    X(ctlOverclockGpuFrequencyOffsetGetV2)                                      \
    X(ctlOverclockGpuFrequencyOffsetSetV2)                                      \
    X(ctlOverclockGpuMaxVoltageOffsetGetV2)                                     \
    X(ctlOverclockGpuMaxVoltageOffsetSetV2)                                     \
    X(ctlOverclockVramMemSpeedLimitGetV2)                                       \
    X(ctlOverclockVramMemSpeedLimitSetV2)                                       \
    X(ctlOverclockPowerLimitGetV2)                                              \
    X(ctlOverclockPowerLimitSetV2)                                              \
    X(ctlOverclockTemperatureLimitGetV2)                                        \
    X(ctlOverclockTemperatureLimitSetV2)                                        \
    X(ctlOverclockResetToDefault)                                               \
    X(ctlOverclockReadVFCurve)                                                  \
    X(ctlOverclockWriteCustomVFCurve)

// Loaded ControlLib.dll + resolved function pointers. One instance is created by
// ArcController; do not construct directly elsewhere.
class IgclLib {
public:
    IgclLib() = default;
    ~IgclLib() { unload(); }

    IgclLib(const IgclLib&) = delete;
    IgclLib& operator=(const IgclLib&) = delete;

    // Loads ControlLib.dll and resolves every entry in ARC_IGCL_FUNCTIONS.
    // Returns false (with error() set) if the DLL is missing or a symbol is
    // absent (older ControlLib without the V2 overclock surface).
    bool load();
    void unload();
    bool loaded() const { return module_ != nullptr; }
    const std::string& error() const { return error_; }

    // Function-pointer members, populated by load(). We bind to the pfn typedefs
    // the IGCL header already declares (ctl_pfn<Name>_t), so a ControlLib ABI
    // bump comes in through the header with no changes here.
    ctl_pfnInit_t                              ctlInit = nullptr;
    ctl_pfnClose_t                             ctlClose = nullptr;
    ctl_pfnEnumerateDevices_t                  ctlEnumerateDevices = nullptr;
    ctl_pfnGetDeviceProperties_t               ctlGetDeviceProperties = nullptr;
    ctl_pfnEnumFans_t                          ctlEnumFans = nullptr;
    ctl_pfnFanGetProperties_t                  ctlFanGetProperties = nullptr;
    ctl_pfnFanGetConfig_t                      ctlFanGetConfig = nullptr;
    ctl_pfnFanSetDefaultMode_t                 ctlFanSetDefaultMode = nullptr;
    ctl_pfnFanSetFixedSpeedMode_t              ctlFanSetFixedSpeedMode = nullptr;
    ctl_pfnFanSetSpeedTableMode_t              ctlFanSetSpeedTableMode = nullptr;
    ctl_pfnFanGetState_t                       ctlFanGetState = nullptr;
    ctl_pfnPowerTelemetryGet_t                 ctlPowerTelemetryGet = nullptr;
    ctl_pfnEnumTemperatureSensors_t            ctlEnumTemperatureSensors = nullptr;
    ctl_pfnTemperatureGetProperties_t          ctlTemperatureGetProperties = nullptr;
    ctl_pfnTemperatureGetState_t               ctlTemperatureGetState = nullptr;
    ctl_pfnOverclockGetProperties_t            ctlOverclockGetProperties = nullptr;
    ctl_pfnOverclockWaiverSet_t                ctlOverclockWaiverSet = nullptr;
    ctl_pfnOverclockGpuFrequencyOffsetGetV2_t  ctlOverclockGpuFrequencyOffsetGetV2 = nullptr;
    ctl_pfnOverclockGpuFrequencyOffsetSetV2_t  ctlOverclockGpuFrequencyOffsetSetV2 = nullptr;
    ctl_pfnOverclockGpuMaxVoltageOffsetGetV2_t ctlOverclockGpuMaxVoltageOffsetGetV2 = nullptr;
    ctl_pfnOverclockGpuMaxVoltageOffsetSetV2_t ctlOverclockGpuMaxVoltageOffsetSetV2 = nullptr;
    ctl_pfnOverclockVramMemSpeedLimitGetV2_t   ctlOverclockVramMemSpeedLimitGetV2 = nullptr;
    ctl_pfnOverclockVramMemSpeedLimitSetV2_t   ctlOverclockVramMemSpeedLimitSetV2 = nullptr;
    ctl_pfnOverclockPowerLimitGetV2_t          ctlOverclockPowerLimitGetV2 = nullptr;
    ctl_pfnOverclockPowerLimitSetV2_t          ctlOverclockPowerLimitSetV2 = nullptr;
    ctl_pfnOverclockTemperatureLimitGetV2_t    ctlOverclockTemperatureLimitGetV2 = nullptr;
    ctl_pfnOverclockTemperatureLimitSetV2_t    ctlOverclockTemperatureLimitSetV2 = nullptr;
    ctl_pfnOverclockResetToDefault_t           ctlOverclockResetToDefault = nullptr;
    ctl_pfnOverclockReadVFCurve_t              ctlOverclockReadVFCurve = nullptr;
    ctl_pfnOverclockWriteCustomVFCurve_t       ctlOverclockWriteCustomVFCurve = nullptr;

private:
    HMODULE module_ = nullptr;
    std::string error_;
};

} // namespace arc
