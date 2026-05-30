"""Central configuration: all SMOKEMON_* env vars, node identity, paths, render constants."""

import os
import shutil
import socket

HOME = os.path.expanduser("~")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _semi_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(";") if x.strip()]


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
# Node config file install.sh writes (KEY=value lines; read by the systemd shipper). Used by
# `smoke hub` to show/repoint where this node ships. Overridable for tests / non-default layouts.
ENV_FILE = os.environ.get("SMOKEMON_ENV_FILE", "/etc/smokemon.env")

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
# CPU temperature (degC) at which most SoCs begin thermal throttling; used by the
# render-side "headroom to throttle" death clock. Pi default is ~80-85C.
THROTTLE_TEMP_C = _f("SMOKEMON_THROTTLE_TEMP", "80")

# synthetic transactions (X6); opt-in scripted multi-step checks beyond single-shot
# probes. Off by default so smokemon makes no extra external requests unless asked.
SYNTHETIC_ENABLED = os.environ.get("SMOKEMON_SYNTHETIC", "0") != "0"
SYNTHETIC_DOH_URL = os.environ.get("SMOKEMON_DOH_URL", "https://cloudflare-dns.com/dns-query")
SYNTHETIC_DOH_NAME = os.environ.get("SMOKEMON_DOH_NAME", "example.com")
# A plain-HTTP endpoint that should answer 204 with an empty body; anything else
# (a 200 + login page, a redirect) means a captive portal / interception.
SYNTHETIC_CAPTIVE_URL = os.environ.get("SMOKEMON_CAPTIVE_URL",
                                       "http://connectivitycheck.gstatic.com/generate_204")

# External lightweight HTTP scrapes. Off by default; when set, collect slow polls explicit
# endpoints only. Format:
#   name=url[|kind=json|metrics][|metrics=a,b,c]
# multiple endpoints are separated by semicolons. No log streaming, Docker scans, or
# background tails: each scrape is bounded by timeout/body/metric limits.
EXT_HTTP = _semi_list("SMOKEMON_EXT_HTTP", "")
EXT_INTERVAL = _f("SMOKEMON_EXT_INTERVAL", "300")
EXT_TIMEOUT = _f("SMOKEMON_EXT_TIMEOUT", "2")
EXT_MAX_BYTES = _i("SMOKEMON_EXT_MAX_BYTES", str(256 * 1024))
EXT_MAX_METRICS = _i("SMOKEMON_EXT_MAX_METRICS", "20")

# Redis stream/queue health. Off by default; implemented as tiny RESP socket reads
# instead of redis-cli or Docker/log inspection. Streams are comma-separated; groups use
# stream=group pairs separated by semicolons.
REDIS_ENABLED = os.environ.get("SMOKEMON_REDIS", "0") != "0"
REDIS_HOST = os.environ.get("SMOKEMON_REDIS_HOST", "127.0.0.1")
REDIS_PORT = _i("SMOKEMON_REDIS_PORT", "6379")
REDIS_TIMEOUT = _f("SMOKEMON_REDIS_TIMEOUT", "1")
REDIS_INTERVAL = _f("SMOKEMON_REDIS_INTERVAL", "60")
REDIS_STREAMS = _list("SMOKEMON_REDIS_STREAMS", "")
REDIS_GROUPS = _semi_list("SMOKEMON_REDIS_GROUPS", "")

# iperf3 (one-shot); set SMOKEMON_IPERF_SERVER to a reachable `iperf3 -s` host
IPERF_SERVER = os.environ.get("SMOKEMON_IPERF_SERVER", "")
IPERF_DURATION = os.environ.get("SMOKEMON_IPERF_DURATION", "5")

# push/webhook alerting (S4); set SMOKEMON_NOTIFY_URL to an ntfy topic, a Slack/Discord
# incoming webhook, or any URL that accepts a JSON {title, body} POST. Kind is
# auto-detected from the host. Only incidents at or above NOTIFY_MIN_SEVERITY fire.
NOTIFY_URL = os.environ.get("SMOKEMON_NOTIFY_URL", "")
NOTIFY_KIND = os.environ.get("SMOKEMON_NOTIFY_KIND", "")  # "" = auto-detect
NOTIFY_MIN_SEVERITY = _i("SMOKEMON_NOTIFY_MIN_SEVERITY", "2")

# central aggregation; set SMOKEMON_HUB_URL to the hub's /ingest endpoint
HUB_URL = os.environ.get("SMOKEMON_HUB_URL", "")
HUB_SECRET = os.environ.get("SMOKEMON_HUB_SECRET", "changeme")
SHIP_BATCH = _i("SMOKEMON_SHIP_BATCH", "2000")
SHIP_INTERVAL = _f("SMOKEMON_SHIP_INTERVAL", "0")  # 0 = drain once and exit
# Raw per-ping rtts stay node-local by default: the hub renders percentile bands from the
# pre-aggregated rtt_min/p25/median/p75/max in ping_runs and never reads raw ping_rtts for
# fresh rows, so shipping them is ~85% of ship traffic for zero hub-side gain. Opt in if a
# hub-side consumer ever needs the raw distribution.
SHIP_RTTS = os.environ.get("SMOKEMON_SHIP_RTTS", "0") != "0"
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
PANELS = ["ping", "net", "http", "mtr", "wifi", "iperf",
          "host", "gpu", "redis", "disk", "thermal", "power", "tcp", "psi", "freq", "self"]
