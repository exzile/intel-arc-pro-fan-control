# Changelog

All notable changes to this project. Dates are ISO (UTC).

## v1.1.2 - 2026-07-09

### Fixed
- **Windows OC gate is now capability-based, not device-id-based.**
  `windows/src/gui/main.cpp` used to blanket-lock all B70/G31 cards out of
  overclocking. It now checks IGCL `bSupported` + a live VF-curve read per
  adapter, so an OC-capable B70 (e.g. an ASRock Arc Pro B70 Creator, subsystem
  `1849:6020`) is no longer wrongly locked out, while firmware-locked cards
  (Intel-reference B70, `8086:1701`) stay correctly gated. Linux (`xe_gt_oc`)
  was already capability-based via `vf_curve` presence.

### Docs
- Corrected the B70/G31 overclocking story across README + docs: OC on B70 is
  **board/firmware-specific** (ASRock provisioned vs. Intel-reference locked),
  auto-detected at runtime rather than blocked by device id. Added the
  board-policy analysis plus PCGamesHardware and issue #1 corroboration to
  `docs/B70-G31-MULTI-GPU.md`, and reconciled `windows/README.md`,
  `windows/PORT.md`, `windows/installer/README.md`, and `docs/NOTES.md` to
  match. The ASRock-enable path itself has not been tested on ASRock hardware
  in this repo — it rides the same capability branch the B60 validates.

## v1.1.1 - 2026-07-09

### Fixed
- **VF curve is 86 points, not 85.** `xe_gt_oc` now reads/writes the full
  86-point voltage-frequency table (index `0x00..0x55`); the reader reports
  exactly the points the firmware exposes and no longer fails the whole read on
  a short/again-gated point. Confirmed against a live Windows-KMD DTrace (86
  writes + 86 reads) and verified on Arc Pro B60 hardware (index `0x55` =
  1035 mV). (#1, #2)
- **`scripts/build-xe-module.sh` no longer reports a false build failure.** The
  symbol-verify step used `nm ... | grep -q` under `set -o pipefail`; the
  SIGPIPE produced a spurious "xe_gt_oc_init missing from module" even when the
  symbol was present. Switched to `grep -c` (matching `xe-fan-rebuild.sh`).

### Changed
- Corrected `docs/B70-G31-MULTI-GPU.md`: the B70/G31 does **not** refuse every
  `0x5d/0x5e/0x5f` opcode. The 3-step begin's `0x5f/4` is accepted; the wall is
  specifically `0x5f/3`. Reads use `p2=0x13`, apply is `0x5e/8/0x73`, and the
  table is 86 points.
- Updated README / OVERCLOCKING / GUI docs and code comments from 85 to 86
  points.
- Reconciled the Windows installer version (`1.0.1` -> `1.1.1`).

### Added
- `oc/pcode_probe` debug sysfs (root, write-only): issue a single PCODE mailbox
  transaction and log `ret/out0/out1`, for characterizing a card's OC surface.

### Investigated (no functional change)
- Hardware-verified issue #1's B70 custom-VF report: the `0x13` read domain,
  `0x5e/8/0x73` apply, and 86-point table are all confirmed. Opening the B70
  write session needs an unpublished "Pcode policy/waiver" before `0x5f/3`, and
  it appears board/firmware specific: it works on the reporter's ASRock
  (`1849:6020`) card, but an Intel-reference (`8086:1701`) B70 refuses the
  verbatim sequence at `0x5f/3`, with its entire `0x5d` VF domain unprovisioned
  from boot.

## v1.1.0 — 2026-07-09

Benchmarking, cross-card support, and no-prompt authorization.

### Added
- **Overclock benchmarking (opt-in).** The stability test can now also measure **FPS**, **VRAM
  bandwidth** and **compute (TFLOPS)** (`clpeak`), plus **real LLM tokens/sec** — prefill + decode —
  via OpenVINO GenAI running on the Arc GPU. Setup is one-time and consent-gated
  (`scripts/setup-llm-benchmark.sh`: Intel OpenCL + `clpeak` + a self-contained Python 3.12 env with
  a small INT4 model under `~/ovbench`).
- **Table result modal vs a stock baseline.** Results render as a Metric / This-run / Stock / Δ
  table with per-metric ▲/▼ percent deltas (coloured good/bad), compared against a saved **stock
  baseline**. A **Stock bench** button records that baseline (runs the full benchmark at stock
  transiently, without changing your applied settings); the modal offers to run one if you benchmark
  an overclock and no baseline exists yet.
- **LLM-output coherence check.** An unstable *memory* overclock can keep tok/s high while silently
  corrupting results. The benchmark now inspects the generated text (unique-word ratio + repeat
  streak) and treats gibberish under an overclock as a **failure → auto-revert to stock** (at stock
  it's flagged as a likely model-setup issue instead).
- **Cross-card support.** Stock mem/temp defaults are **read from the driver and persisted per-card**
  (`~/.config/xe-gpu-arc/stock.json`, keyed by GPU id) instead of assuming the B60's values; on a
  clean boot the live values are captured automatically. Benchmark records are stamped with the GPU
  id and compared only within the same card.
- **Passwordless helpers (polkit).** `install.sh` installs a scoped rule
  (`/etc/polkit-1/rules.d/49-xe-gpu.rules`) so the GPU-control helpers run without a `pkexec` prompt
  for a locally logged-in admin. Limited to those specific binaries; package installs and every other
  action still prompt, and SSH/remote sessions are never covered.
- **Live status modal** with per-phase progress (load → clpeak → LLM) during the test.
- **Failed-boot watchdog** so a bad overclock can never cause a boot loop (`xe-gpu-oc-confirm.service`).

### Changed
- The stability test applies your **current (even un-Applied) settings transiently** for the test and
  restores whatever was live afterwards — so you can validate a setting before committing it.
- `docs/GPU-TUNING.md` rewritten with the tuning reality: a positive voltage offset doesn't raise
  clocks, `rp0` is a hard wall, memory headroom is often near zero, and an undervolt is the one
  useful lever on a maxed-out card (efficiency, not speed).

### Fixed
- Result modal never appeared on a passing benchmark (a misplaced tail crashed `_bench_save`).
- A stock run wasn't recognized as the baseline (it got a settings hash instead of the `stock` key),
  giving a permanent "No stock baseline yet".
- False "unstable" verdict from benign per-engine resets (`clpeak`/LLM compute engine) being counted
  as hangs; stability is now judged before the benchmark runs.
- Wayland-aware load generator (plain `glmark2` is X11-only → use `glmark2-wayland`/`vkmark`).

## v1.0.1 — 2026-07-07
Installer/packaging fixes and the first tagged desktop release.

## v1.0.0
Initial release — fan curves, power/clock tuning, VF-curve overclock, metrics dashboard, multi-GPU.
