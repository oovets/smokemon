"""Central configuration: all SMOKEMON_* env vars, node identity, paths, render constants."""

import os
import shutil
import socket

HOME = os.path.expanduser("~")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _f(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: str) -> int:
    return int(os.environ.get(name, default))


def cli_path(env_var: str, name: str) -> str:
    """Resolve a CLI: explicit env var -> PATH lookup -> bare name (resolved at exec)."""
    return os.environ.get(env_var) or shutil.which(name) or name


NODE = os.environ.get("SMOKEMON_NODE") or socket.gethostname()
DB_PATH = os.environ.get("SMOKEMON_DB", os.path.join(HOME, "smokemon", "data", "smokemon.db"))
HUB_DB = os.environ.get("SMOKEMON_HUB_DB", os.path.join(HOME, "smokemon", "data", "smokemon-hub.db"))

# ping + net (fast loop)
TARGETS = _list("SMOKEMON_TARGETS", "1.1.1.1,192.168.0.1")
PING_INTERVAL = _f("SMOKEMON_INTERVAL", "10")
PING_COUNT = _i("SMOKEMON_COUNT", "20")
PING_PERIOD = _i("SMOKEMON_PERIOD", "50")

# http / mtr / wifi (slow loop)
PROBE_INTERVAL = _f("SMOKEMON_PROBE_INTERVAL", "60")
HTTP_URLS = _list("SMOKEMON_HTTP_URLS", "https://www.google.com,https://www.cloudflare.com")
MTR_TARGETS = _list("SMOKEMON_MTR_TARGETS", "1.1.1.1")
MTR_COUNT = _i("SMOKEMON_MTR_COUNT", "10")
WIFI_ENABLED = os.environ.get("SMOKEMON_WIFI", "1") != "0"

# host health
HOST_INTERVAL = _f("SMOKEMON_HOST_INTERVAL", "30")
PROC_TOPN = _i("SMOKEMON_PROC_TOPN", "5")

# iperf3 (one-shot); set SMOKEMON_IPERF_SERVER to a reachable `iperf3 -s` host
IPERF_SERVER = os.environ.get("SMOKEMON_IPERF_SERVER", "")
IPERF_DURATION = os.environ.get("SMOKEMON_IPERF_DURATION", "5")

# central aggregation; set SMOKEMON_HUB_URL to the hub's /ingest endpoint
HUB_URL = os.environ.get("SMOKEMON_HUB_URL", "")
HUB_SECRET = os.environ.get("SMOKEMON_HUB_SECRET", "changeme")
SHIP_BATCH = _i("SMOKEMON_SHIP_BATCH", "2000")
SHIP_INTERVAL = _f("SMOKEMON_SHIP_INTERVAL", "0")  # 0 = drain once and exit
HUB_BIND = os.environ.get("SMOKEMON_HUB_BIND", "0.0.0.0")
HUB_PORT = _i("SMOKEMON_HUB_PORT", "8765")
HUB_MAX_BODY = _i("SMOKEMON_HUB_MAX_BODY", str(64 * 1024 * 1024))

# CLI tool paths (env -> PATH)
FPING = cli_path("SMOKEMON_FPING", "fping")
MTR = cli_path("SMOKEMON_MTR", "mtr")
IPERF = cli_path("SMOKEMON_IPERF", "iperf3")
MTR_SUDO = os.environ.get("SMOKEMON_MTR_SUDO", "1") != "0"

# render; map your target IPs to friendly names here
TARGET_LABELS = {"1.1.1.1": "internet", "192.168.0.1": "gw"}
HTTP_COLORS = ["cyan", "green+", "magenta+", "blue+", "orange+"]
PANELS = ["ping", "net", "http", "mtr", "wifi", "iperf", "host", "disk"]
