// igcl.cpp — implementation of the ControlLib.dll dynamic loader.
#include "igcl.hpp"

namespace arc {

bool IgclLib::load() {
    if (module_) return true;

    // ControlLib.dll ships with the Intel graphics driver and is normally on the
    // default DLL search path (System32). LoadLibraryW resolves it there.
    module_ = ::LoadLibraryW(L"ControlLib.dll");
    if (!module_) {
        error_ =
            "ControlLib.dll not found. It is installed by the Intel graphics "
            "driver; install/repair the Intel Arc driver, or copy ControlLib.dll "
            "next to this executable.";
        return false;
    }

    // Required exports: absence is fatal.
    std::string missing;
#define ARC_LOAD_REQ(name)                                                      \
    name = reinterpret_cast<decltype(name)>(                                    \
        ::GetProcAddress(module_, #name));                                      \
    if (!name) missing += " " #name;
    ARC_IGCL_REQUIRED(ARC_LOAD_REQ)
#undef ARC_LOAD_REQ

    if (!missing.empty()) {
        error_ = "ControlLib.dll is missing required exports (update the Intel "
                 "Arc driver):" + missing;
        unload();
        return false;
    }

    // Optional exports: resolve if present, leave null otherwise.
#define ARC_LOAD_OPT(name)                                                      \
    name = reinterpret_cast<decltype(name)>(                                    \
        ::GetProcAddress(module_, #name));
    ARC_IGCL_OPTIONAL(ARC_LOAD_OPT)
#undef ARC_LOAD_OPT

    return true;
}

void IgclLib::unload() {
    if (module_) {
        ::FreeLibrary(module_);
        module_ = nullptr;
    }
}

} // namespace arc
