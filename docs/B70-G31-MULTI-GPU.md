# Arc Pro B70 (Battlemage G31) + multi-GPU — getting it to bind, and OC findings

This documents adding a second card — an **Arc Pro B70 (Battlemage G31, `8086:e223`, 32 GB)** —
alongside the existing **B60 (G21, `8086:e211`, 24 GB)**, on an older **ASUS ROG STRIX Z370-H
(Coffee Lake)** board. Two separate problems: **(1)** getting the B70 to bind to `xe` at all, and
**(2)** whether the reverse-engineered OC path works on the G31 die. TL;DR: **(1) solved and made
persistent; (2) the G31 rejects the whole OC-write PCODE surface — fan/power/clock work, the VF
curve / mem-speed / temp-limit do not.**

Reference: kernel `7.0.0-27-generic`, Linux `xe` driver, both cards on CPU PCIe root ports.

---

## 1. Getting the B70 to bind (the 32 GB BAR problem)

### Symptom
With the B70 installed, `xe` claims only the B60. The B70 (`07:00.0`) is unbound; dmesg shows:

```
xe 0000:07:00.0: Attempting to resize bar from 0MiB -> 32768MiB
xe 0000:07:00.0: Can't resize VRAM BAR - platform support is missing.
                 Consider enabling 'Resizable BAR' support in your BIOS
xe 0000:07:00.0: *ERROR* failed to map registers
xe 0000:07:00.0: probe with driver xe failed with error -5
```

### Root cause
The B70 POSTs with a **32 GB** physical VRAM BAR (BAR2) **and** a **32 GB SR-IOV VF BAR** — ~64 GB
of prefetchable MMIO. On a board without **Above-4G Decoding**, that can't be placed below 4 GB, so
the kernel builds **no** bridge window for the B70's bus — not even for its 16 MB register BAR — and
probe fails with `-EIO`. The B60 never hit this because it POSTs a small **256 MB** BAR (it has run
in "small-BAR" mode all along; `VRAM 24GB is larger than resource 256MB`).

**Enabling Above-4G in the BIOS is not a viable fix here:** the 32 GB window then starves other
devices of MMIO — on this board it made the **NVMe boot drive disappear** ("PCIe resource error"),
i.e. unbootable. (The SR-IOV VF BARs are a red herring: they "fail to assign" gracefully on *both*
cards and are non-fatal — the B60 works with its VF BARs unassigned too.)

### Fix — shrink the BAR, no BIOS change
We don't need the big BAR: fan control, telemetry, and power/clock tuning never touch VRAM directly.
So we make the B70 use a **256 MB** BAR like the B60:

1. Program the B70's **PCIe Resizable BAR control register** (BAR2 size field → 256 MB) directly:
   ```
   setpci -s <bdf> 0x428.L            # read; low 3 bits = BAR index (2), bits[13:8] = size (0xf=32GB)
   setpci -s <bdf> 0x428.L=0x00000822 # bits[13:8]=8 => 2^8 MB = 256 MB
   ```
2. **`kexec`** into the same kernel. kexec skips the PCIe reset, so the 256 MB setting **survives**,
   and the fresh enumeration maps the card cleanly below 4 GB (there's ~800 MB free there). The B70
   then binds, exposes `oc/*`, and NVMe + B60 are untouched.

Live rescans don't work (the CPU root port's prefetchable window is `[disabled]` and can't be
re-enabled at runtime); a normal reboot resets the card back to 32 GB. kexec is what threads the
needle. Also add to the kernel cmdline (via GRUB) so the driver keeps it small and quiets the VF
noise:

```
pci=realloc=on xe.vram_bar_size=256 xe.max_vfs=0
```

### Persistence (survives cold reboots automatically)
`install.sh` installs **`xe-b70-rebar-kexec.sh`** + **`xe-b70-rebar.service`**. On every cold boot
the service detects the unbound `e223`, shrinks its BAR, and does a **one-time kexec** (guarded by a
`xe_b70_kexeced=1` cmdline flag so it can never loop). Cost: ~15–20 s extra on cold boot. Needs
`kexec-tools`.

- Log: `/var/log/xe-b70-rebar.log`
- Disable: `sudo systemctl disable xe-b70-rebar.service` (the B70 then simply won't bind on this
  board until re-enabled).
- Recovery if a boot ever wedges: at the GRUB menu press `e`, boot once; then disable the service.

A **BIOS alternative** exists if your board has enough MMIO: *Above 4G Decoding = Enabled* +
*Re-Size BAR Support = Disabled* (so the card keeps a small default BAR) + *CSM = Disabled*. On this
Z370 that still risked the NVMe, so the kexec route is preferred here.

---

## 2. Overclocking on the G31 — what works and what doesn't

Once bound, the B70 was exercised against the toolkit. Summary:

| Capability | B70 (G31) |
|---|---|
| Telemetry (clocks / temps / power / fan) | works |
| **Fan control** (`xe-fan-curve` max/curve/auto) | **works** — PCODE fan op `0x7d` accepted; 750 → 3493 rpm |
| Power cap + clock limits (`xe-gpu-tune`, driver sysfs) | works, reversible |
| **VF voltage curve / mem-speed / temp-limit (PCODE OC)** | **rejected by firmware (`-71`)** |
| Multi-GPU isolation (writes to one card never touch the other) | verified (fan + clock) |

### The OC finding (measured with a PCODE probe)
A debug `oc/pcode_probe` sysfs (issues one mailbox command and reports `ret/data0/data1`) was added
to `xe_gt_oc.c` to compare the two dies directly:

| PCODE op | meaning | B60 (G21) | B70 (G31) |
|---|---|---|---|
| `0x5c` p1=0 | late-binding capability read | `ret=0`, `0x9` | `ret=0`, `0x9` (**identical**) |
| `0x5d/8/3` | VF-curve point **read** | `ret=0` (real mV) | **`ret=-71`** |
| `0x5f/2` | VF-curve **begin** session | ok | **`ret=-71`** |
| `0x5e/6/0x17` | memory-speed **set** | `ret=0` | **`ret=-71`** |
| `0x5e/6/0x49` | temp-limit **set** | `ret=0` | **`ret=-71`** |

Interpretation:
- The **general PCODE mailbox works** on the B70 — `0x5c` returns the *same* `0x9` capability word
  (fan + VR **supported, not provisioned**) as the B60.
- But **every Intel-private late-binding OC opcode** our `xe_gt_oc` patch uses (`0x5d`, `0x5e`,
  `0x5f`) is refused with **`-71` (EPROTO / illegal subcommand)** on the G31, where the B60 accepts
  them. It is *not* a session/ordering issue (the `begin` itself is rejected) and *not* the
  documented "nothing staged yet" `-71` (the *writes* are rejected, and the B60's identical writes
  succeed with `ret=0`).
- So the `mem_speed`/`temp_limit` values the tools report on the B70 are the **stock fallbacks** the
  driver substitutes on read error, not live reads.

**Conclusion:** the G31 does **not** expose OC through the same late-binding opcodes as the G21. The
capability word is the same, so this is a firmware-behaviour difference, not a capability/fusing one.
Most likely the shipped B70 GSC/PCODE firmware implements OC under a **different opcode set / unlock
sequence** (or gates it differently on the workstation SKU).

### Reverse-engineering status / roadmap
This is the same wall we started at for the B60, and the same tool breaks it:

1. **Firmware comparison (done — read-only, via `igsc`).** The two cards run **different die-specific
   GSC firmware**, which is where these late-binding opcodes live:

   | | B60 (G21) | B70 (G31) |
   |---|---|---|
   | GSC FW version | `BMG__21.1182` | `BMG__31.1058` |
   | fw-data (OEM config) | Format 2, v203, mfg 7 | Format 2, v203, mfg 44 |
   | OPROM code | `17 00 2A 04 …` | `17 00 29 04 …` |

   So the OC rejection tracks a **firmware build difference**, not a capability/fusing one (the `0x5c`
   cap word is identical). The `BMG_31.1058` build rejects the opcodes the `BMG_21.1182` build accepts.
   Built with `intel/igsc` + `intel/metee`; `igsc list-devices` maps MEI↔BDF (mei1=B60, mei2=B70).

   **A newer G31 firmware exists but is only a marginal bump.** Extracted the GSC images from the
   latest Intel driver (`gfx_win_101.8804.exe`, Q2.26 — its NSIS/RAR SFX only unpacks on Windows;
   `igsc fw version --image` reads the bundled version):

   | | on-card | bundled in 8804 driver |
   |---|---|---|
   | B70 (G31) | `BMG__31.1058` | **`BMG__31.1062`** (4 builds newer) |
   | B60 (G21) | `BMG__21.1182` | `BMG__21.1182` (already current) |

   Static analysis is inconclusive: **both** images contain a `PCODE_0` / `PCODE_0.met` module (the
   PCODE firmware where the OC opcodes live *is* present on the G31), and the rest is signed/compressed
   (no readable OC strings). So the gate is inside the compiled PCODE logic — a 4-build bump *could*
   flip it but far more likely carries minor fixes, and Pro-SKU OC gating may be intentional. We
   **did not flash** (user directive + documented Gen5→Gen4 permanent-downgrade risk on this card).
   Whether `1062` enables OC is only knowable by flashing (off the table) or by #2 below.
2. **Definitive: DTrace the B70's Windows driver** while its OC UI applies a change (exactly how the
   B60 sequence — the missing `0x5f/2` begin — was found). Captures the G31's real
   opcode/domain/sequence. Needs Windows + the card + DTrace.
3. Cross-check the captured ops against `xe_pcode_api.h` names and IGCL's `ctlOverclock*` surface.

The `oc/pcode_probe` debug interface (in the patched module) is the bench for validating whatever
sequence #2 turns up — it can issue any candidate op safely (single shots; **no** blind enumeration,
which has wedged the mailbox → GPU hang before).

---

## Notes
- Both cards report **PCIe Gen1 x1** at idle — a **reporting artifact** (idle link downtrain), not a
  defect; it retrains under load.
- Small-BAR mode costs nothing for this toolkit (no direct large-VRAM CPU mapping needed).
- The patched module (`oc/pcode_probe`) is **unsigned / out-of-tree** (kernel taint `12288`), built
  in EXTMOD mode against the tree's `Module.symvers`. Revert to the stock signed `xe.ko` when the RE
  bench is no longer needed.
