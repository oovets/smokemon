#!/bin/sh
# smokemon one-line bootstrap installer (Linux node or hub). Clones the repo and runs
# deploy/install_linux.sh. Requires the repo to be reachable (public, or set SMOKEMON_REPO
# to an authenticated URL). Run as root via a pipe:
#
#   NODE: curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
#           | sudo bash -s -- --node NAME --hub-url http://HUB-HOST:8765/ingest --secret SECRET
#   HUB:  curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
#           | sudo bash -s -- --hub --secret SECRET
#
# Override clone location with SMOKEMON_DIR (default /opt/smokemon).
set -eu

REPO="${SMOKEMON_REPO:-https://github.com/oovets/smokemon.git}"
DIR="${SMOKEMON_DIR:-/opt/smokemon}"

[ "$(id -u)" -eq 0 ] || { echo "smokemon: run as root, e.g. | sudo bash -s -- --node NAME ..." >&2; exit 1; }

if ! command -v git >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y --no-install-recommends git
fi
if [ -d "$DIR/.git" ]; then
    echo "==> updating $DIR"; git -C "$DIR" pull -q --ff-only
else
    echo "==> cloning $REPO -> $DIR"; git clone -q --depth 1 "$REPO" "$DIR"
fi

exec bash "$DIR/deploy/install_linux.sh" "$@"
