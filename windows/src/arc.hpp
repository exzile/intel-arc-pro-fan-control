// arc.hpp — high-level Intel Arc control wrapper over IGCL.
//
// This is the Windows analogue of the Linux toolkit's sysfs/PCODE layer. Where
// Linux drives the GPU's PCODE mailbox directly (a patched xe.ko), Windows goes
// through Intel's own Graphics Control Library (IGCL / ControlLib.dll), which
// exposes the same capabilities as first-class userspace APIs:
//
//   Linux (this repo)                 ->  Windows (IGCL)
//   pwm1_auto_point* fan curve        ->  ctlFanSetSpeedTableMode
//   pwm1_enable = 0 (full)            ->  ctlFanSetFixedSpeedMode 100%
//   pwm1_enable = 2 (auto/stock)      ->  ctlFanSetDefaultMode
//   xe_gt_oc vf_curve (0x5d/0x5f)     ->  ctlOverclockRead/WriteCustomVFCurve
//   oc/mem_speed (0x5e/0x17)          ->  ctlOverclockVramMemSpeedLimitSetV2
//   oc/temp_limit (0x5e/0x49)         ->  ctlOverclockTemperatureLimitSetV2
//   power1_cap                        ->  ctlOverclockPowerLimitSetV2
//   energy*_input (power derivation)  ->  ctlPowerTelemetryGet energy counters
//
// A note on units: the Linux fan curve uses PWM 0-255; IGCL fan tables use
// RPM or PERCENT. This port speaks PERCENT (0-100) as the native fan unit.
#pragma once

#include <string>
#include <vector>
#include <cstdint>
#include "igcl.hpp"

namespace arc {

// Intel PCI vendor id — used to filter the adapter list to Arc / Iris Xe parts.
constexpr uint32_t kIntelVendorId = 0x8086;

struct AdapterInfo {
    ctl_device_adapter_handle_t handle = nullptr;
    std::string name;
    uint32_t pciVendorId = 0;
    uint32_t pciDeviceId = 0;
    ctl_adapter_bdf_t bdf{};
    bool isIntel() const { return pciVendorId == kIntelVendorId; }
    // "bus:device.function", e.g. "03:00.0" — matches the Linux ARC_GPU_BDF tail.
    std::string bdfString() const;
    // Stable, unique-per-model key for per-adapter profiles. BDF is unreliable on
    // Windows (IGCL reports 00:00.0 for every adapter), so we key on the PCI device
    // id: "e211" = Arc Pro B60, "e223" = Arc Pro B70.
    std::string key() const;
};

struct FanProperties {
    bool canControl = false;
    int  maxRPM = -1;
    int  maxPoints = -1;      // max temp/speed pairs the hw fan table supports
    bool supportsTable = false;
    bool supportsFixed = false;
    bool supportsPercent = false;
    bool supportsRPM = false;
};

// A single fan-curve point in this port's native unit (temperature -> percent).
struct FanPoint {
    int temperatureC = 0;
    int speedPercent = 0;
};

// One VF-curve point (matches ctl_voltage_frequency_point_t).
struct VFPoint {
    uint32_t voltageMv = 0;
    uint32_t freqMHz = 0;
};

// One temperature sensor reading.
struct TempSensor {
    std::string label;     // "gpu" / "vram" / "global" / ...
    double currentC = 0;   // -1 if unreadable
    double maxC = 0;       // hardware max for this sensor (0 if unknown)
};

// Aggregated VRAM state across all device-local memory modules.
struct MemoryInfo {
    uint64_t totalBytes = 0;   // allocatable total
    uint64_t freeBytes = 0;
    uint64_t usedBytes = 0;
    int busWidth = -1;
    int numChannels = -1;
    bool valid = false;
};

// A raw telemetry snapshot. Rate metrics (power, utilization, bandwidth) are
// counters here; call deriveMetrics() on two snapshots to get instantaneous
// values, exactly as the Linux tools do with energy*_input.
struct Telemetry {
    bool valid = false;
    double timeStampSec = 0;

    double gpuFreqMHz = 0;      bool hasGpuFreq = false;
    double gpuTempC = 0;        bool hasGpuTemp = false;
    double vramTempC = 0;       bool hasVramTemp = false;
    double gpuVoltageV = 0;     bool hasGpuVoltage = false;
    double fanRpm = 0;          bool hasFanRpm = false;

    double gpuEnergyJoules = 0; bool hasGpuEnergy = false;
    double cardEnergyJoules = 0;bool hasCardEnergy = false;

    double globalActivitySec = 0; bool hasGlobalActivity = false;
    double renderActivitySec = 0; bool hasRenderActivity = false;
    double mediaActivitySec = 0;  bool hasMediaActivity = false;

    double vramReadBwCounter = 0;  bool hasVramReadBw = false;
    double vramWriteBwCounter = 0; bool hasVramWriteBw = false;

    bool powerLimited = false;
    bool tempLimited = false;
    bool currentLimited = false;
    bool voltageLimited = false;
    bool utilLimited = false;
};

// Instantaneous metrics derived from two telemetry snapshots (b is later).
struct Metrics {
    double gpuFreqMHz = 0;
    double gpuTempC = 0;
    double vramTempC = 0;
    double gpuVoltageV = 0;
    double fanRpm = 0;
    double gpuPowerW = 0;      bool hasGpuPower = false;
    double cardPowerW = 0;     bool hasCardPower = false;
    double gpuUtilPct = 0;     bool hasGpuUtil = false;
    double renderUtilPct = 0;  bool hasRenderUtil = false;
    double mediaUtilPct = 0;   bool hasMediaUtil = false;
    double vramReadBwMBps = 0; bool hasVramReadBw = false;
    double vramWriteBwMBps = 0;bool hasVramWriteBw = false;
    bool powerLimited = false;
    bool tempLimited = false;
    bool voltageLimited = false;
    bool currentLimited = false;
    bool utilLimited = false;
};

// Snapshot of the current overclock knobs + the hardware's advertised ranges.
struct OcState {
    bool supported = false;
    ctl_oc_properties_t props{};   // ranges/units/defaults per control
    double gpuFreqOffset = 0;      bool hasGpuFreqOffset = false;
    double gpuVoltOffset = 0;      bool hasGpuVoltOffset = false;
    double memSpeed = 0;           bool hasMemSpeed = false;
    double powerLimitW = 0;        bool hasPowerLimit = false;
    double tempLimitC = 0;         bool hasTempLimit = false;
};

class ArcController {
public:
    ArcController() = default;
    ~ArcController() { shutdown(); }

    ArcController(const ArcController&) = delete;
    ArcController& operator=(const ArcController&) = delete;

    // Load ControlLib, ctlInit, and enumerate Intel adapters. On success at
    // least one adapter is present and one is selected (see selection rules).
    bool init(std::string& err);
    void shutdown();

    const std::vector<AdapterInfo>& adapters() const { return adapters_; }
    const AdapterInfo* current() const;

    // Selection. init() auto-selects the ARC_GPU_BDF env target if set, else the
    // first Intel adapter. Callers may override.
    bool selectByIndex(size_t i, std::string& err);
    bool selectByBdf(const std::string& bdf, std::string& err);
    // Select by AdapterInfo::key() (PCI device id hex, e.g. "e223"). The reliable
    // multi-GPU selector on Windows since BDF is ambiguous.
    bool selectByKey(const std::string& key, std::string& err);

    // --- Fan ---------------------------------------------------------------
    bool fanProperties(FanProperties& out, std::string& err);
    bool fanSetCurve(const std::vector<FanPoint>& pts, std::string& err);
    bool fanSetFixed(int percent, std::string& err);   // "max" = fanSetFixed(100)
    bool fanSetAuto(std::string& err);                 // stock/default mode
    bool fanGetRpm(int& rpm, std::string& err);
    bool fanGetPercent(int& percent, std::string& err);
    bool fanGetCurve(std::vector<FanPoint>& out, std::string& err);

    // --- Telemetry ---------------------------------------------------------
    bool sampleTelemetry(Telemetry& out, std::string& err);
    static Metrics deriveMetrics(const Telemetry& a, const Telemetry& b);

    // Per-sensor temperatures (GPU / VRAM / global + min variants). Requires the
    // ControlLib temperature exports; returns false if they're absent.
    bool readTemperatures(std::vector<TempSensor>& out, std::string& err);

    // VRAM used/total, aggregated across device-local memory modules. On Linux
    // this needed root-only debugfs; IGCL exposes it directly.
    bool readMemory(MemoryInfo& out, std::string& err);

    // --- Overclock / tuning ------------------------------------------------
    bool ocGetState(OcState& out, std::string& err);
    bool ocSetWaiver(std::string& err);                 // required before V/VF writes
    bool setGpuFreqOffset(double value, std::string& err);
    bool setGpuVoltageOffset(double value, std::string& err);
    bool setMemSpeed(double value, std::string& err);
    bool setPowerLimit(double watts, std::string& err);
    bool setTempLimit(double celsius, std::string& err);
    bool ocReset(std::string& err);
    bool readVFCurve(std::vector<VFPoint>& out, bool live, std::string& err);
    bool writeVFCurve(const std::vector<VFPoint>& pts, std::string& err);

private:
    ctl_device_adapter_handle_t handle() const;
    bool ensureFanHandle(std::string& err);

    IgclLib lib_;
    ctl_api_handle_t api_ = nullptr;
    std::vector<AdapterInfo> adapters_;
    size_t current_ = static_cast<size_t>(-1);
    ctl_fan_handle_t fan_ = nullptr;    // first controllable fan of current adapter
};

} // namespace arc
