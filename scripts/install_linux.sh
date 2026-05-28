#!/usr/bin/env bash
# smokemon Linux-installer (Raspberry Pi / Jetson / Debian-likt). Installerar beroenden,
# ger fping/mtr CAP_NET_RAW (slipper sudo), skriver /etc/smokemon.env och installerar
# systemd-units. Kör som root (sudo).
#
#   NOD:  sudo scripts/install_linux.sh --node pi-vardagsrum \
#             --hub-url http://100.87.219.2:8765/ingest --secret DIN_HEMLIGHET
#   HUBB: sudo scripts/install_linux.sh --hub --secret DIN_HEMLIGHET
#
# Noden kör collector/probes/host + skeppar var 60:e sek + iperf var 15:e min.
# Hubben kör hub_ingest. --secret måste matcha mellan nod och hubb.
set -euo pipefail

MODE="node"
NODE_NAME="$(hostname)"
HUB_URL="http://100.87.219.2:8765/ingest"
SECRET=""
TARGETS="1.1.1.1,192.168.0.1"

while [ $# -gt 0 ]; do
    case "$1" in
        --hub) MODE="hub"; shift ;;
        --node) NODE_NAME="$2"; shift 2 ;;
        --hub-url) HUB_URL="$2"; shift 2 ;;
        --secret) SECRET="$2"; shift 2 ;;
        --targets) TARGETS="$2"; shift 2 ;;
        *) echo "okänt argument: $1" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Kör som root (sudo)." >&2
    exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(whoami)}"
PYTHON="$(command -v python3)"
ENV_FILE="/etc/smokemon.env"
UNIT_DIR="/etc/systemd/system"

echo "==> smokemon-install: mode=$MODE dir=$DIR user=$USER_NAME python=$PYTHON"

echo "==> apt-beroenden"
apt-get update -qq
if [ "$MODE" = "hub" ]; then
    apt-get install -y --no-install-recommends iperf3 python3-matplotlib python3-numpy
else
    apt-get install -y --no-install-recommends fping iperf3 iw mtr-tiny
fi

if [ "$MODE" = "node" ]; then
    echo "==> CAP_NET_RAW på fping/mtr (slipper sudo)"
    for bin in "$(command -v fping || true)" "$(command -v mtr-packet || true)" "$(command -v mtr || true)"; do
        [ -n "$bin" ] && setcap cap_net_raw+ep "$bin" 2>/dev/null && echo "    setcap $bin" || true
    done
    echo "==> plotext för lokal TUI (valfritt; collectors behöver det ej)"
    sudo -u "$USER_NAME" "$PYTHON" -m pip install --user plotext 2>/dev/null \
        || sudo -u "$USER_NAME" "$PYTHON" -m pip install --user --break-system-packages plotext 2>/dev/null \
        || echo "    (plotext-install hoppades över — installera manuellt för TUI)"
fi

echo "==> skriver $ENV_FILE"
{
    echo "# smokemon-konfiguration (genererad av install_linux.sh)"
    echo "SMOKEMON_DB=$DIR/data/smokemon.db"
    echo "SMOKEMON_NODE=$NODE_NAME"
    if [ "$MODE" = "hub" ]; then
        echo "SMOKEMON_HUB_DB=$DIR/data/smokemon-hub.db"
        echo "SMOKEMON_HUB_BIND=0.0.0.0"
        echo "SMOKEMON_HUB_PORT=8765"
        echo "SMOKEMON_HUB_SECRET=$SECRET"
    else
        echo "SMOKEMON_TARGETS=$TARGETS"
        echo "SMOKEMON_MTR_SUDO=0"
        echo "SMOKEMON_HUB_URL=$HUB_URL"
        echo "SMOKEMON_HUB_SECRET=$SECRET"
    fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
mkdir -p "$DIR/data"
chown -R "$USER_NAME" "$DIR/data"

install_unit() {
    local name="$1"
    sed -e "s|__USER__|$USER_NAME|g" \
        -e "s|__SMOKEMON_DIR__|$DIR|g" \
        -e "s|__PYTHON__|$PYTHON|g" \
        -e "s|__ENV_FILE__|$ENV_FILE|g" \
        "$DIR/systemd/$name" > "$UNIT_DIR/$name"
    echo "    installerade $name"
}

echo "==> systemd-units"
if [ "$MODE" = "hub" ]; then
    install_unit smokemon-hub-ingest.service
    systemctl daemon-reload
    systemctl enable --now smokemon-hub-ingest.service
    echo "==> klart. Hubben lyssnar på :8765. Kontroll: systemctl status smokemon-hub-ingest"
else
    for u in smokemon-collector.service smokemon-probes.service smokemon-host.service \
             smokemon-iperf.service smokemon-iperf.timer \
             smokemon-shipper.service smokemon-shipper.timer; do
        install_unit "$u"
    done
    systemctl daemon-reload
    systemctl enable --now smokemon-collector.service smokemon-probes.service smokemon-host.service \
        smokemon-iperf.timer smokemon-shipper.timer
    echo "==> klart. Kontroll: systemctl status 'smokemon-*' ; journalctl -u smokemon-collector -f"
fi
