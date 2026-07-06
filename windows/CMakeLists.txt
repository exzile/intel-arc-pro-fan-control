# Arc Fan Control — Windows port (IGCL backend).
#
# Builds two executables:
#   arc-gpu          — command-line control (fan / tune / overclock / status)
#   arc-fan-service  — Windows service that re-applies the saved profile at boot
#
# Requires the IGCL public header at third_party/igcl/igcl_api.h — see that
# directory's README for how to obtain it. No import library is needed:
# ControlLib.dll (from the Intel driver) is loaded dynamically at runtime.
cmake_minimum_required(VERSION 3.20)
project(ArcFanControl LANGUAGES CXX)

if(NOT WIN32)
    message(FATAL_ERROR "The Windows port builds on Windows only (MSVC/clang-cl).")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(IGCL_HEADER "${CMAKE_CURRENT_SOURCE_DIR}/third_party/igcl/igcl_api.h")
if(NOT EXISTS "${IGCL_HEADER}")
    message(FATAL_ERROR
        "IGCL header not found at:\n  ${IGCL_HEADER}\n"
        "Obtain igcl_api.h from https://github.com/intel/drivers.gpu.control-library "
        "and place it there (see third_party/igcl/README.md).")
endif()

# Shared core: IGCL loader + Arc wrapper + config + profile apply.
add_library(arc_core STATIC
    src/igcl.cpp
    src/arc.cpp
    src/fan_curve.cpp
    src/config.cpp
    src/apply.cpp
)
target_include_directories(arc_core PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/third_party/igcl"
)
# Unicode Win32 APIs (RegisterServiceCtrlHandlerExW etc.).
target_compile_definitions(arc_core PUBLIC UNICODE _UNICODE)
if(MSVC)
    target_compile_options(arc_core PRIVATE /W4 /permissive-)
endif()

add_executable(arc-gpu src/cli/main.cpp)
target_link_libraries(arc-gpu PRIVATE arc_core)

add_executable(arc-fan-service src/service/service_main.cpp)
target_link_libraries(arc-fan-service PRIVATE arc_core advapi32)

# Native Win32/GDI dashboard. WIN32 => GUI subsystem (wWinMain, no console).
add_executable(arc-gpu-gui WIN32 src/gui/main.cpp)
target_link_libraries(arc-gpu-gui PRIVATE arc_core comctl32)

install(TARGETS arc-gpu arc-fan-service arc-gpu-gui RUNTIME DESTINATION bin)
