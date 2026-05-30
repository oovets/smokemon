#!/usr/bin/env bash
# Pull the latest code into this checkout and restart smokemon's services (node and/or hub).
# Deployed everywhere via git, so the same command works on every host: ~/smokemon/scripts/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> git pull"
git pull --ff-only
if command -v systemctl >/dev/null 2>&1; then
    # restart only the long-running units that exist here (collectors on every node; hub +
    # iperf-server on the hub). The shipper/iperf timers re-read code+env on their next fire.
    units=""
    for u in smokemon-hub smokemon-iperf-server smokemon-collect-fast smokemon-collect-slow; do
        systemctl cat "$u.service" >/dev/null 2>&1 && units="$units $u"
    done
    [ -n "$units" ] && { echo "==> restart$units"; sudo systemctl restart $units; }
else
    # macOS / launchd
    U=$(id -u)
    for a in collect-fast collect-slow shipper; do
        launchctl kickstart -k "gui/$U/com.smokemon.$a" 2>/dev/null || true
    done
    echo "==> kickstarted launchd agents"
fi
echo "==> done ($(git rev-parse --short HEAD))"
