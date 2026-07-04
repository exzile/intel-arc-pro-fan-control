# Bundled kernel patch — attribution & license

`xe-fan-control-168027-cachyos-7.1.2.patch` is a modification of the Linux kernel
`drm/xe` driver and is therefore licensed **GPL-2.0**, the same as the kernel.

- **Origin:** Intel patch series **168027**, *"[PATCH v1 0/5] Add fan control support"*,
  author **Karthik Poosa**, posted to `intel-xe@lists.freedesktop.org` (June 2026).
  https://patchwork.freedesktop.org/series/168027/
- **This adaptation** (rebased for CachyOS 7.1.2; also applies to Ubuntu 7.0.0 with fuzz) is from
  https://github.com/PerkyZZ999/XeDriver_FanPatch .

It is bundled here only to make the toolkit self-contained. All copyright and the GPL-2.0 license
remain with the original authors. If/when series 168027 merges into mainline Linux, this bundled
copy becomes unnecessary — prefer the upstream/distro kernel.
