#!/usr/bin/env bash
# Pull the latest code into this checkout and restart smokemon's services (node and/or hub).
# Deployed everywhere via git, so the same command works on every host: ~/smokemon/scripts/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> git pull"
git pull --ff-only
if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemctl not found. smokemon is Linux/systemd-only; see INSTALL.md." >&2
    exit 1
fi
# restart only the long-running units that exist here (collectors on every node; hub on the
# hub). The shipper/prune timers re-read code+env on their next fire.
units=""
for u in smokemon-hub smokemon-collect-fast smokemon-collect-slow; do
    systemctl cat "$u.service" >/dev/null 2>&1 && units="$units $u"
done
[ -n "$units" ] && { echo "==> restart$units"; sudo systemctl restart $units; }
echo "==> done ($(git rev-parse --short HEAD))"
