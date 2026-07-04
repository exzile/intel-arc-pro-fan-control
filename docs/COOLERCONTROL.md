# CoolerControl (GUI fan curves)

Once the patched module is loaded, [CoolerControl](https://gitlab.com/coolercontrol/coolercontrol)
sees the Arc fan as a controllable device and gives you a graphical curve editor that persists.

## Install (Debian/Ubuntu, official CloudSmith repo)
```bash
sudo apt install curl
curl -1sLf 'https://dl.cloudsmith.io/public/coolercontrol/coolercontrol/setup.deb.sh' | sudo -E bash
sudo apt update && sudo apt install coolercontrol
sudo systemctl enable --now coolercontrold
```
(CloudSmith serves all Ubuntu codenames, incl. very new ones.) Launch **CoolerControl** from your
app menu, or open `http://localhost:11987`.

## Make a curve
1. **Profiles → + Add Profile** → type **Graph**.
2. **Temp source**: the GPU temperature under the **xe** device.
3. Drag points to shape temp→% (e.g. 40→20, 55→40, 65→60, 75→80, 85→100). Save.
4. On the dashboard, set the **fan channel** (under the xe GPU) from *Default* to your new profile. Apply.
CoolerControl then drives the fan continuously along your curve and re-applies at boot.

## Interaction with this toolkit's systemd curve
- At boot, `xe-fan-curve.service` applies a safe baseline curve *before* CoolerControl's daemon starts.
- When you configure CoolerControl to manage the fan, it takes over (writes `pwm1` continuously).
- Keep both (recommended — the systemd curve is a safety net if CoolerControl isn't running), or
  `sudo systemctl disable xe-fan-curve.service` if you want CoolerControl to be the sole manager.
- If CoolerControl shows the fan as read-only again, a kernel update reverted the module —
  run `sudo xe-fan-rebuild` and reboot.
