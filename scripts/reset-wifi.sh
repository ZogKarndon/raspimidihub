#!/bin/bash
# Block Wi-Fi on an Ethernet-only RaspiMIDIHub.
# Older images may still have a client profile. This drops those
# profiles and rfkills the radio. It does not start an access point.
# Usage: sudo reset-wifi
set -e

echo "Blocking Wi-Fi. The hub stays on Ethernet."

mount -o remount,rw /

# Remove any saved WiFi client connections (keep wired)
for f in /etc/NetworkManager/system-connections/*.nmconnection; do
    [ -f "$f" ] || continue
    if grep -q "type=wifi" "$f"; then
        echo "  Removing $(basename "$f")"
        rm -f "$f"
    fi
done

# Drop a legacy wifi block from the config if one is still on disk.
CONFIG="/boot/firmware/raspimidihub/config.json"
if [ -f "$CONFIG" ]; then
    python3 -c "
import json
with open('$CONFIG') as f:
    cfg = json.load(f)
if 'wifi' in cfg:
    cfg.pop('wifi', None)
    with open('$CONFIG', 'w') as f:
        json.dump(cfg, f, indent=2)
    print('  Removed legacy wifi config')
else:
    print('  No wifi config to remove')
"
fi

rfkill block wifi 2>/dev/null || true
nmcli radio wifi off 2>/dev/null || true

mount -o remount,ro /

echo "Restarting service..."
systemctl restart raspimidihub
echo "Done. Reach the UI on Ethernet (http://raspimidihub-<id>.local/)."
