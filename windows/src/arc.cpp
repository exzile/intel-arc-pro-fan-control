// arc.cpp — implementation of the IGCL-backed Arc controller.
#include "arc.hpp"

#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <algorithm>

// Guard an optional IGCL entry point: if this ControlLib.dll didn't export it,
// fail with a clear message instead of calling through a null pointer.
#define ARC_REQUIRE_FN(fn)                                                      \
    do {                                                                        \
        if (!lib_.fn) {                                                         \
            err = "this Intel driver's ControlLib does not expose " #fn         \
                  " — the overclock/tuning API is unavailable; update the "     \
                  "Intel Arc driver.";                                          \
            return false;                                                       \
        }                                                                       \
    } while (0)

namespace arc {
namespace {

// Zero a versioned IGCL struct and stamp its Size. Version stays 0 (baseline),
// which every current ControlLib accepts; bump per-call if a newer field set is
// needed.
template <typename T>
void zeroInit(T& s) {
    std::memset(&s, 0, sizeof(T));
    s.Size = static_cast<uint32_t>(sizeof(T));
}

// Convert a telemetry item's tagged union to a double regardless of its type.
double itemToDouble(const ctl_oc_telemetry_item_t& it) {
    switch (it.type) {
        case CTL_DATA_TYPE_INT8:   return static_cast<double>(it.value.data8);
        case CTL_DATA_TYPE_UINT8:  return static_cast<double>(it.value.datau8);
        case CTL_DATA_TYPE_INT16:  return static_cast<double>(it.value.data16);
        case CTL_DATA_TYPE_UINT16: return static_cast<double>(it.value.datau16);
        case CTL_DATA_TYPE_INT32:  return static_cast<double>(it.value.data32);
        case CTL_DATA_TYPE_UINT32: return static_cast<double>(it.value.datau32);
        case CTL_DATA_TYPE_INT64:  return static_cast<double>(it.value.data64);
        case CTL_DATA_TYPE_UINT64: return static_cast<double>(it.value.datau64);
        case CTL_DATA_TYPE_FLOAT:  return static_cast<double>(it.value.datafloat);
        case CTL_DATA_TYPE_DOUBLE: return it.value.datadouble;
        default:                   return 0.0;
    }
}

std::string ctlErr(ctl_result_t r) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "IGCL error 0x%08x", static_cast<unsigned>(r));
    return buf;
}

} // namespace

std::string AdapterInfo::bdfString() const {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%02x:%02x.%x", bdf.bus, bdf.device, bdf.function);
    return buf;
}

std::string AdapterInfo::key() const {
    char buf[8];
    std::snprintf(buf, sizeof(buf), "%04x", pciDeviceId);
    return buf;
}

bool ArcController::init(std::string& err) {
    if (!lib_.load()) {
        err = lib_.error();
        return false;
    }

    ctl_init_args_t args;
    zeroInit(args);
    args.AppVersion = CTL_MAKE_VERSION(CTL_IMPL_MAJOR_VERSION, CTL_IMPL_MINOR_VERSION);
    // Level Zero backend is required for the Sysman surface (fans, temperature,
    // power, frequency); without it ctlEnumFans returns CTL_RESULT_ERROR_ZE_LOADER.
    args.flags = CTL_INIT_FLAG_USE_LEVEL_ZERO;

    ctl_result_t r = lib_.ctlInit(&args, &api_);
    if (r != CTL_RESULT_SUCCESS || api_ == nullptr) {
        err = "ctlInit failed (" + ctlErr(r) +
              "). Ensure a supported Intel Arc driver is installed.";
        return false;
    }

    // Enumerate all adapters, keep the Intel ones.
    uint32_t count = 0;
    r = lib_.ctlEnumerateDevices(api_, &count, nullptr);
    if (r != CTL_RESULT_SUCCESS || count == 0) {
        err = "No graphics adapters reported by IGCL (" + ctlErr(r) + ").";
        return false;
    }
    std::vector<ctl_device_adapter_handle_t> handles(count, nullptr);
    r = lib_.ctlEnumerateDevices(api_, &count, handles.data());
    if (r != CTL_RESULT_SUCCESS) {
        err = "ctlEnumerateDevices failed (" + ctlErr(r) + ").";
        return false;
    }

    adapters_.clear();
    for (uint32_t i = 0; i < count; ++i) {
        if (!handles[i]) continue;
        ctl_device_adapter_properties_t props;
        zeroInit(props);
        LUID luid{};                      // IGCL writes the OS device id here
        props.pDeviceID = &luid;
        props.device_id_size = sizeof(luid);
        if (lib_.ctlGetDeviceProperties(handles[i], &props) != CTL_RESULT_SUCCESS)
            continue;
        if (props.pci_vendor_id != kIntelVendorId)
            continue;                     // skip AMD/NVIDIA/other adapters

        AdapterInfo a;
        a.handle = handles[i];
        a.name = props.name;              // NUL-terminated in the struct
        a.pciVendorId = props.pci_vendor_id;
        a.pciDeviceId = props.pci_device_id;
        a.bdf = props.adapter_bdf;
        adapters_.push_back(a);
    }

    if (adapters_.empty()) {
        err = "No Intel graphics adapter found.";
        return false;
    }

    // Selection: honour ARC_GPU_BDF (matches the Linux tools' multi-GPU env),
    // else first Intel adapter.
    current_ = 0;
    if (const char* bdf = std::getenv("ARC_GPU_BDF")) {
        std::string ignore;
        selectByBdf(bdf, ignore);        // falls back to index 0 on no match
    }
    return true;
}

void ArcController::shutdown() {
    // Do NOT ctlClose here: with the Level Zero backend, ctlClose (and unloading
    // ControlLib) crashes during Level Zero teardown. Drop our references and let
    // the OS reclaim everything at process exit (this is a long-lived service /
    // a short-lived CLI, so leaking until exit is fine).
    api_ = nullptr;
    fan_ = nullptr;
    adapters_.clear();
    lib_.unload();
}

const AdapterInfo* ArcController::current() const {
    if (current_ >= adapters_.size()) return nullptr;
    return &adapters_[current_];
}

ctl_device_adapter_handle_t ArcController::handle() const {
    const AdapterInfo* a = current();
    return a ? a->handle : nullptr;
}

bool ArcController::selectByIndex(size_t i, std::string& err) {
    if (i >= adapters_.size()) {
        err = "adapter index out of range";
        return false;
    }
    if (i != current_) {
        current_ = i;
        fan_ = nullptr;   // re-resolve fan handle for the new adapter lazily
    }
    return true;
}

bool ArcController::selectByBdf(const std::string& bdf, std::string& err) {
    // Accept a full "domain:bus:device.function" or just the "bus:device.function"
    // tail; compare against our short form.
    for (size_t i = 0; i < adapters_.size(); ++i) {
        const std::string s = adapters_[i].bdfString();
        const bool tailMatch = bdf.size() >= s.size() &&
                               bdf.compare(bdf.size() - s.size(), s.size(), s) == 0;
        if (bdf == s || tailMatch) {
            return selectByIndex(i, err);
        }
    }
    err = "no adapter matching BDF '" + bdf + "'";
    return false;
}

bool ArcController::selectByKey(const std::string& key, std::string& err) {
    for (size_t i = 0; i < adapters_.size(); ++i) {
        if (adapters_[i].key() == key) return selectByIndex(i, err);
    }
    err = "no adapter with key '" + key + "'";
    return false;
}

bool ArcController::ensureFanHandle(std::string& err) {
    if (fan_) return true;
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }

    uint32_t count = 0;
    ctl_result_t r = lib_.ctlEnumFans(h, &count, nullptr);
    if (r != CTL_RESULT_SUCCESS || count == 0) {
        err = "this adapter exposes no controllable fan (" + ctlErr(r) + ").";
        return false;
    }
    std::vector<ctl_fan_handle_t> fans(count, nullptr);
    r = lib_.ctlEnumFans(h, &count, fans.data());
    if (r != CTL_RESULT_SUCCESS) { err = "ctlEnumFans failed (" + ctlErr(r) + ")."; return false; }
    fan_ = fans[0];
    return fan_ != nullptr;
}

bool ArcController::fanProperties(FanProperties& out, std::string& err) {
    if (!ensureFanHandle(err)) return false;
    ctl_fan_properties_t p;
    zeroInit(p);
    ctl_result_t r = lib_.ctlFanGetProperties(fan_, &p);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlFanGetProperties failed (" + ctlErr(r) + ")."; return false; }
    out.canControl = p.canControl;
    out.maxRPM = p.maxRPM;
    out.maxPoints = p.maxPoints;
    out.supportsTable = (p.supportedModes & (1u << CTL_FAN_SPEED_MODE_TABLE)) != 0;
    out.supportsFixed = (p.supportedModes & (1u << CTL_FAN_SPEED_MODE_FIXED)) != 0;
    out.supportsPercent = (p.supportedUnits & (1u << CTL_FAN_SPEED_UNITS_PERCENT)) != 0;
    out.supportsRPM = (p.supportedUnits & (1u << CTL_FAN_SPEED_UNITS_RPM)) != 0;
    return true;
}

bool ArcController::fanSetCurve(const std::vector<FanPoint>& pts, std::string& err) {
    if (!ensureFanHandle(err)) return false;
    if (pts.empty()) { err = "fan curve has no points"; return false; }
    if (pts.size() > CTL_FAN_TEMP_SPEED_PAIR_COUNT) {
        err = "fan curve has too many points (max " +
              std::to_string(CTL_FAN_TEMP_SPEED_PAIR_COUNT) + ")";
        return false;
    }
    // IGCL requires the table sorted by temperature ascending.
    std::vector<FanPoint> sorted(pts);
    std::sort(sorted.begin(), sorted.end(),
              [](const FanPoint& a, const FanPoint& b) {
                  return a.temperatureC < b.temperatureC;
              });

    // Enforce a MONOTONIC (non-decreasing) speed curve. A point whose speed drops
    // below a cooler point's (e.g. a mis-dragged GUI curve like 62:85 -> 65:24) is
    // an invalid fan table: the driver can accept the SpeedTableMode call, silently
    // ignore it, and leave the fan stuck in a no-control state until a driver reset.
    // Clamp each speed up to the running maximum so the table is always valid.
    int runningMax = 0;
    for (FanPoint& p : sorted) {
        if (static_cast<int>(p.speedPercent) < runningMax)
            p.speedPercent = static_cast<decltype(p.speedPercent)>(runningMax);
        runningMax = static_cast<int>(p.speedPercent);
    }

    ctl_fan_speed_table_t table;
    zeroInit(table);
    table.numPoints = static_cast<int32_t>(sorted.size());
    for (size_t i = 0; i < sorted.size(); ++i) {
        ctl_fan_temp_speed_t& e = table.table[i];
        zeroInit(e);
        e.temperature = static_cast<uint32_t>(sorted[i].temperatureC);
        zeroInit(e.speed);
        e.speed.speed = sorted[i].speedPercent;
        e.speed.units = CTL_FAN_SPEED_UNITS_PERCENT;
    }
    ctl_result_t r = lib_.ctlFanSetSpeedTableMode(fan_, &table);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlFanSetSpeedTableMode failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::fanSetFixed(int percent, std::string& err) {
    // These cards don't support FIXED fan mode (ctlFanSetFixedSpeedMode returns
    // CTL_RESULT_ERROR_UNSUPPORTED_FEATURE) — only the speed TABLE works. Emulate
    // a constant speed with a flat curve (same percent at every temperature).
    percent = std::max(0, std::min(100, percent));
    std::vector<FanPoint> flat = {
        {0,   percent}, {40, percent}, {70, percent}, {100, percent},
    };
    return fanSetCurve(flat, err);
}

bool ArcController::fanSetAuto(std::string& err) {
    // Do NOT use ctlFanSetDefaultMode. It relinquishes our software ownership of
    // the fan, and the public IGCL fan API cannot re-acquire control afterward in
    // the same driver session: ctlFanSetSpeedTableMode then returns SUCCESS but
    // silently no-ops (the driver keeps the hardware/stock curve) until a full
    // driver reset. Intel's own app avoids this by driving the fan via the private
    // DXGK escape 0x80c rather than the public ownership state machine.
    //
    // Instead, emulate "auto" with Intel's stock temperature curve applied via
    // table mode. This gives the same near-silent-at-idle behaviour while KEEPING
    // us in control, so subsequent curve changes still take effect without a reset.
    static const std::vector<FanPoint> kStockCurve = {
        {59, 0}, {60, 20}, {65, 30}, {70, 40}, {75, 60},
        {79, 80}, {84, 100}, {90, 100}, {94, 100}, {95, 100},
    };
    return fanSetCurve(kStockCurve, err);
}

bool ArcController::fanGetRpm(int& rpm, std::string& err) {
    if (!ensureFanHandle(err)) return false;
    int32_t v = -1;
    ctl_result_t r = lib_.ctlFanGetState(fan_, CTL_FAN_SPEED_UNITS_RPM, &v);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlFanGetState(rpm) failed (" + ctlErr(r) + ")."; return false; }
    rpm = v;
    return true;
}

bool ArcController::fanGetPercent(int& percent, std::string& err) {
    if (!ensureFanHandle(err)) return false;
    int32_t v = -1;
    ctl_result_t r = lib_.ctlFanGetState(fan_, CTL_FAN_SPEED_UNITS_PERCENT, &v);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlFanGetState(percent) failed (" + ctlErr(r) + ")."; return false; }
    percent = v;
    return true;
}

bool ArcController::fanGetCurve(std::vector<FanPoint>& out, std::string& err) {
    if (!ensureFanHandle(err)) return false;
    ctl_fan_config_t cfg;
    zeroInit(cfg);
    zeroInit(cfg.speedFixed);
    zeroInit(cfg.speedTable);
    ctl_result_t r = lib_.ctlFanGetConfig(fan_, &cfg);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlFanGetConfig failed (" + ctlErr(r) + ")."; return false; }
    out.clear();
    for (int32_t i = 0; i < cfg.speedTable.numPoints && i < CTL_FAN_TEMP_SPEED_PAIR_COUNT; ++i) {
        FanPoint p;
        p.temperatureC = static_cast<int>(cfg.speedTable.table[i].temperature);
        p.speedPercent = cfg.speedTable.table[i].speed.speed;
        out.push_back(p);
    }
    return true;
}

bool ArcController::sampleTelemetry(Telemetry& out, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ctl_power_telemetry_t t;
    zeroInit(t);
    ctl_result_t r = lib_.ctlPowerTelemetryGet(h, &t);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlPowerTelemetryGet failed (" + ctlErr(r) + ")."; return false; }

    out = Telemetry{};
    out.valid = true;
    if (t.timeStamp.bSupported) out.timeStampSec = itemToDouble(t.timeStamp);
    if (t.gpuCurrentClockFrequency.bSupported) { out.gpuFreqMHz = itemToDouble(t.gpuCurrentClockFrequency); out.hasGpuFreq = true; }
    if (t.gpuCurrentTemperature.bSupported) { out.gpuTempC = itemToDouble(t.gpuCurrentTemperature); out.hasGpuTemp = true; }
    if (t.vramCurrentTemperature.bSupported) { out.vramTempC = itemToDouble(t.vramCurrentTemperature); out.hasVramTemp = true; }
    if (t.gpuVoltage.bSupported) { out.gpuVoltageV = itemToDouble(t.gpuVoltage); out.hasGpuVoltage = true; }
    if (t.fanSpeed[0].bSupported) { out.fanRpm = itemToDouble(t.fanSpeed[0]); out.hasFanRpm = true; }
    if (t.gpuEnergyCounter.bSupported) { out.gpuEnergyJoules = itemToDouble(t.gpuEnergyCounter); out.hasGpuEnergy = true; }
    if (t.totalCardEnergyCounter.bSupported) { out.cardEnergyJoules = itemToDouble(t.totalCardEnergyCounter); out.hasCardEnergy = true; }
    if (t.globalActivityCounter.bSupported) { out.globalActivitySec = itemToDouble(t.globalActivityCounter); out.hasGlobalActivity = true; }
    if (t.renderComputeActivityCounter.bSupported) { out.renderActivitySec = itemToDouble(t.renderComputeActivityCounter); out.hasRenderActivity = true; }
    if (t.mediaActivityCounter.bSupported) { out.mediaActivitySec = itemToDouble(t.mediaActivityCounter); out.hasMediaActivity = true; }
    if (t.vramReadBandwidthCounter.bSupported) { out.vramReadBwCounter = itemToDouble(t.vramReadBandwidthCounter); out.hasVramReadBw = true; }
    if (t.vramWriteBandwidthCounter.bSupported) { out.vramWriteBwCounter = itemToDouble(t.vramWriteBandwidthCounter); out.hasVramWriteBw = true; }
    out.powerLimited = t.gpuPowerLimited;
    out.tempLimited = t.gpuTemperatureLimited;
    out.currentLimited = t.gpuCurrentLimited;
    out.voltageLimited = t.gpuVoltageLimited;
    out.utilLimited = t.gpuUtilizationLimited;
    return true;
}

Metrics ArcController::deriveMetrics(const Telemetry& a, const Telemetry& b) {
    Metrics m;
    m.gpuFreqMHz = b.gpuFreqMHz;
    m.gpuTempC = b.gpuTempC;
    m.vramTempC = b.vramTempC;
    m.gpuVoltageV = b.gpuVoltageV;
    m.fanRpm = b.fanRpm;
    m.powerLimited = b.powerLimited;
    m.tempLimited = b.tempLimited;
    m.voltageLimited = b.voltageLimited;
    m.currentLimited = b.currentLimited;
    m.utilLimited = b.utilLimited;

    const double dt = b.timeStampSec - a.timeStampSec;
    if (dt <= 0.0) return m;   // need a positive interval to differentiate counters

    if (a.hasGpuEnergy && b.hasGpuEnergy) {
        m.gpuPowerW = (b.gpuEnergyJoules - a.gpuEnergyJoules) / dt;
        m.hasGpuPower = true;
    }
    if (a.hasCardEnergy && b.hasCardEnergy) {
        m.cardPowerW = (b.cardEnergyJoules - a.cardEnergyJoules) / dt;
        m.hasCardPower = true;
    }
    // Activity counters measure seconds-busy; delta/interval = utilization.
    if (a.hasGlobalActivity && b.hasGlobalActivity) {
        m.gpuUtilPct = 100.0 * (b.globalActivitySec - a.globalActivitySec) / dt;
        m.hasGpuUtil = true;
    }
    if (a.hasRenderActivity && b.hasRenderActivity) {
        m.renderUtilPct = 100.0 * (b.renderActivitySec - a.renderActivitySec) / dt;
        m.hasRenderUtil = true;
    }
    if (a.hasMediaActivity && b.hasMediaActivity) {
        m.mediaUtilPct = 100.0 * (b.mediaActivitySec - a.mediaActivitySec) / dt;
        m.hasMediaUtil = true;
    }
    if (a.hasVramReadBw && b.hasVramReadBw) {
        m.vramReadBwMBps = (b.vramReadBwCounter - a.vramReadBwCounter) / dt / 1.0e6;
        m.hasVramReadBw = true;
    }
    if (a.hasVramWriteBw && b.hasVramWriteBw) {
        m.vramWriteBwMBps = (b.vramWriteBwCounter - a.vramWriteBwCounter) / dt / 1.0e6;
        m.hasVramWriteBw = true;
    }
    return m;
}

namespace {
const char* tempSensorLabel(ctl_temp_sensors_t t) {
    switch (t) {
        case CTL_TEMP_SENSORS_GLOBAL:     return "global";
        case CTL_TEMP_SENSORS_GPU:        return "gpu";
        case CTL_TEMP_SENSORS_MEMORY:     return "vram";
        case CTL_TEMP_SENSORS_GLOBAL_MIN: return "global_min";
        case CTL_TEMP_SENSORS_GPU_MIN:    return "gpu_min";
        case CTL_TEMP_SENSORS_MEMORY_MIN: return "vram_min";
        default:                          return "sensor";
    }
}
} // namespace

bool ArcController::readTemperatures(std::vector<TempSensor>& out, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlEnumTemperatureSensors);
    ARC_REQUIRE_FN(ctlTemperatureGetProperties);
    ARC_REQUIRE_FN(ctlTemperatureGetState);

    uint32_t count = 0;
    ctl_result_t r = lib_.ctlEnumTemperatureSensors(h, &count, nullptr);
    if (r != CTL_RESULT_SUCCESS || count == 0) { err = "no temperature sensors (" + ctlErr(r) + ")."; return false; }
    std::vector<ctl_temp_handle_t> handles(count, nullptr);
    r = lib_.ctlEnumTemperatureSensors(h, &count, handles.data());
    if (r != CTL_RESULT_SUCCESS) { err = "ctlEnumTemperatureSensors failed (" + ctlErr(r) + ")."; return false; }

    out.clear();
    for (uint32_t i = 0; i < count; ++i) {
        if (!handles[i]) continue;
        ctl_temp_properties_t props;
        zeroInit(props);
        if (lib_.ctlTemperatureGetProperties(handles[i], &props) != CTL_RESULT_SUCCESS) continue;
        TempSensor s;
        s.label = tempSensorLabel(props.type);
        s.maxC = props.maxTemperature;
        double cur = -1.0;
        if (lib_.ctlTemperatureGetState(handles[i], &cur) == CTL_RESULT_SUCCESS) s.currentC = cur;
        else s.currentC = -1.0;
        out.push_back(s);
    }
    return true;
}

bool ArcController::readMemory(MemoryInfo& out, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlEnumMemoryModules);
    ARC_REQUIRE_FN(ctlMemoryGetState);

    uint32_t count = 0;
    ctl_result_t r = lib_.ctlEnumMemoryModules(h, &count, nullptr);
    if (r != CTL_RESULT_SUCCESS || count == 0) { err = "no memory modules (" + ctlErr(r) + ")."; return false; }
    std::vector<ctl_mem_handle_t> handles(count, nullptr);
    r = lib_.ctlEnumMemoryModules(h, &count, handles.data());
    if (r != CTL_RESULT_SUCCESS) { err = "ctlEnumMemoryModules failed (" + ctlErr(r) + ")."; return false; }

    out = MemoryInfo{};
    for (uint32_t i = 0; i < count; ++i) {
        if (!handles[i]) continue;
        // Properties are optional detail; state carries the total/free we need.
        if (lib_.ctlMemoryGetProperties) {
            ctl_mem_properties_t props;
            zeroInit(props);
            if (lib_.ctlMemoryGetProperties(handles[i], &props) == CTL_RESULT_SUCCESS) {
                if (props.busWidth > 0) out.busWidth = props.busWidth;
                if (props.numChannels > 0) out.numChannels = props.numChannels;
            }
        }
        ctl_mem_state_t st;
        zeroInit(st);
        if (lib_.ctlMemoryGetState(handles[i], &st) == CTL_RESULT_SUCCESS) {
            out.totalBytes += st.size;
            out.freeBytes += st.free;
            out.valid = true;
        }
    }
    if (!out.valid) { err = "memory state unavailable"; return false; }
    out.usedBytes = (out.totalBytes >= out.freeBytes) ? out.totalBytes - out.freeBytes : 0;
    return true;
}

bool ArcController::ocGetState(OcState& out, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockGetProperties);
    zeroInit(out.props);
    ctl_result_t r = lib_.ctlOverclockGetProperties(h, &out.props);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlOverclockGetProperties failed (" + ctlErr(r) + ")."; return false; }
    out.supported = out.props.bSupported;

    double v = 0;
    if (lib_.ctlOverclockGpuFrequencyOffsetGetV2 && lib_.ctlOverclockGpuFrequencyOffsetGetV2(h, &v) == CTL_RESULT_SUCCESS) { out.gpuFreqOffset = v; out.hasGpuFreqOffset = true; }
    if (lib_.ctlOverclockGpuMaxVoltageOffsetGetV2 && lib_.ctlOverclockGpuMaxVoltageOffsetGetV2(h, &v) == CTL_RESULT_SUCCESS) { out.gpuVoltOffset = v; out.hasGpuVoltOffset = true; }
    if (lib_.ctlOverclockVramMemSpeedLimitGetV2 && lib_.ctlOverclockVramMemSpeedLimitGetV2(h, &v) == CTL_RESULT_SUCCESS) { out.memSpeed = v; out.hasMemSpeed = true; }
    if (lib_.ctlOverclockPowerLimitGetV2 && lib_.ctlOverclockPowerLimitGetV2(h, &v) == CTL_RESULT_SUCCESS) { out.powerLimitW = v; out.hasPowerLimit = true; }
    if (lib_.ctlOverclockTemperatureLimitGetV2 && lib_.ctlOverclockTemperatureLimitGetV2(h, &v) == CTL_RESULT_SUCCESS) { out.tempLimitC = v; out.hasTempLimit = true; }
    return true;
}

bool ArcController::ocSetWaiver(std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockWaiverSet);
    ctl_result_t r = lib_.ctlOverclockWaiverSet(h);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlOverclockWaiverSet failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::setGpuFreqOffset(double value, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockGpuFrequencyOffsetSetV2);
    if (!ocSetWaiver(err)) return false;
    ctl_result_t r = lib_.ctlOverclockGpuFrequencyOffsetSetV2(h, value);
    if (r != CTL_RESULT_SUCCESS) { err = "set GPU frequency offset failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::setGpuVoltageOffset(double value, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockGpuMaxVoltageOffsetSetV2);
    // Voltage offset requires the warranty waiver to be set first.
    if (!ocSetWaiver(err)) return false;
    ctl_result_t r = lib_.ctlOverclockGpuMaxVoltageOffsetSetV2(h, value);
    if (r != CTL_RESULT_SUCCESS) { err = "set GPU voltage offset failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::setMemSpeed(double value, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockVramMemSpeedLimitSetV2);
    if (!ocSetWaiver(err)) return false;
    ctl_result_t r = lib_.ctlOverclockVramMemSpeedLimitSetV2(h, value);
    if (r != CTL_RESULT_SUCCESS) { err = "set memory speed failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::setPowerLimit(double watts, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockPowerLimitSetV2);
    // All OC V2 setters require the warranty waiver first (else -> DATA_WRITE).
    if (!ocSetWaiver(err)) return false;
    ctl_result_t r = lib_.ctlOverclockPowerLimitSetV2(h, watts);
    if (r != CTL_RESULT_SUCCESS) { err = "set power limit failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::setTempLimit(double celsius, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockTemperatureLimitSetV2);
    if (!ocSetWaiver(err)) return false;
    ctl_result_t r = lib_.ctlOverclockTemperatureLimitSetV2(h, celsius);
    if (r != CTL_RESULT_SUCCESS) { err = "set temperature limit failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::ocReset(std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockResetToDefault);
    ctl_result_t r = lib_.ctlOverclockResetToDefault(h);
    if (r != CTL_RESULT_SUCCESS) { err = "ctlOverclockResetToDefault failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

bool ArcController::readVFCurve(std::vector<VFPoint>& out, bool live, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockReadVFCurve);
    const ctl_vf_curve_type_t type = live ? CTL_VF_CURVE_TYPE_LIVE : CTL_VF_CURVE_TYPE_STOCK;
    const ctl_vf_curve_details_t detail = CTL_VF_CURVE_DETAILS_ELABORATE;

    uint32_t n = 0;
    ctl_result_t r = lib_.ctlOverclockReadVFCurve(h, type, detail, &n, nullptr);
    if (r != CTL_RESULT_SUCCESS || n == 0) { err = "read VF curve (count) failed (" + ctlErr(r) + ")."; return false; }
    std::vector<ctl_voltage_frequency_point_t> pts(n);
    r = lib_.ctlOverclockReadVFCurve(h, type, detail, &n, pts.data());
    if (r != CTL_RESULT_SUCCESS) { err = "read VF curve failed (" + ctlErr(r) + ")."; return false; }
    out.clear();
    out.reserve(n);
    for (uint32_t i = 0; i < n; ++i) out.push_back({pts[i].Voltage, pts[i].Frequency});
    return true;
}

bool ArcController::writeVFCurve(const std::vector<VFPoint>& pts, std::string& err) {
    ctl_device_adapter_handle_t h = handle();
    if (!h) { err = "no adapter selected"; return false; }
    ARC_REQUIRE_FN(ctlOverclockWriteCustomVFCurve);
    if (pts.empty()) { err = "VF curve has no points"; return false; }
    // Custom VF curve writes require the warranty waiver.
    if (!ocSetWaiver(err)) return false;
    std::vector<ctl_voltage_frequency_point_t> raw(pts.size());
    for (size_t i = 0; i < pts.size(); ++i) { raw[i].Voltage = pts[i].voltageMv; raw[i].Frequency = pts[i].freqMHz; }
    ctl_result_t r = lib_.ctlOverclockWriteCustomVFCurve(h, static_cast<uint32_t>(raw.size()), raw.data());
    if (r != CTL_RESULT_SUCCESS) { err = "write custom VF curve failed (" + ctlErr(r) + ")."; return false; }
    return true;
}

} // namespace arc
