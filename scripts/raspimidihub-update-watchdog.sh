#!/bin/bash
# Restart the routing service if an update was explicitly armed with
# the 180s watchdog. The hub is Ethernet-only; this does not start an
# access point. The update orchestrator does not schedule this unit.
#
# Usage: raspimidihub-update-watchdog.sh <reason>

REASON="${1:-unknown}"
STATUS_FILE="/run/raspimidihub/update-status"

mkdir -p /run/raspimidihub
printf '{"step":"error-watchdog","message":"Update watchdog restarted the service. Reason: %s"}\n' "$REASON" \
    > "$STATUS_FILE.tmp"
mv "$STATUS_FILE.tmp" "$STATUS_FILE"

logger -t raspimidihub-update-watchdog "watchdog firing — reason: $REASON"

systemctl restart raspimidihub.service || true
