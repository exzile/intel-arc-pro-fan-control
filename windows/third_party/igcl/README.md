# IGCL header (not vendored)

This port builds against Intel's **Graphics Control Library** (IGCL) public
header, which is **not committed here** — it is Intel's own source and carries
Intel's license. Drop it in yourself before building:

```
windows/third_party/igcl/igcl_api.h
```

## Where to get it

From Intel's open-source repo
[intel/drivers.gpu.control-library](https://github.com/intel/drivers.gpu.control-library):

```powershell
# from the repo root
curl -L -o windows/third_party/igcl/igcl_api.h `
  https://raw.githubusercontent.com/intel/drivers.gpu.control-library/master/include/igcl_api.h
```

…or clone the repo and copy `include/igcl_api.h` here. Pin to a released tag if
you want a reproducible build.

## Why it isn't bundled

- It is Intel-authored and governed by the license in that repository — we keep
  this repo's own MIT/GPL licensing clean by not redistributing it.
- Pulling it fresh means you always build against a header that matches the
  ControlLib.dll shipped with your installed Intel driver.

## Runtime

No import library is required. `ControlLib.dll` (installed by the Intel Arc
graphics driver) is loaded dynamically at runtime by `src/igcl.cpp`. If it is
missing, the tools print a clear message pointing at the driver install.
