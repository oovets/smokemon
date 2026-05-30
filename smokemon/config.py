"""Central configuration: all SMOKEMON_* env vars, node identity, paths, render constants."""

import os
import shutil
import socket
import subprocess

HOME = os.path.expanduser("~")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _semi_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(";") if x.strip()]


def _f(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: str) -> int:
    return int(os.environ.get(name, default))


def _enabled(name: str, default: bool) -> bool:
    """Tri-state on/off: unset -> default (auto). '0/false/no/off' -> disabled, else on."""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def _forced_on(name: str) -> bool:
    """True only when the var is explicitly set to a truthy value (caller wants the probe
    to run and report even if the dependency is not auto-detected on this node)."""
    v = os.environ.get(name)
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def cli_path(env_var: str, name: str) -> str:
    """Resolve a CLI: explicit env var -> PATH lookup -> bare name (resolved at exec)."""
    return os.environ.get(env_var) or shutil.which(name) or name


NODE = os.environ.get("SMOKEMON_NODE") or socket.gethostname()
DB_PATH = os.environ.get("SMOKEMON_DB", os.path.join(HOME, "smokemon", "data", "smokemon.db"))
HUB_DB = os.environ.get("SMOKEMON_HUB_DB", os.path.join(HOME, "smokemon", "data", "smokemon-hub.db"))
# Node config file install.sh writes (KEY=value lines; read by the systemd shipper). Used by
# `smoke hub` to show/repoint where this node ships. Overridable for tests / non-default layouts.
ENV_FILE = os.environ.get("SMOKEMON_ENV_FILE", "/etc/smokemon.env")

def default_gateway() -> str | None:
    """The node's IPv4 default-gateway IP, stdlib-only. Lets every node ping its own first hop
    without hardcoding a per-site LAN address. Linux reads /proc/net/route; macOS/BSD parse
    `route -n get default`. Returns None if it can't be determined."""
    try:  # Linux: the default route is the line with destination 00000000
        with open("/proc/net/route") as f:
            for line in f.read().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 0x2:
                    h = fields[2]  # gateway, little-endian hex -> dotted quad
                    return ".".join(str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0))
    except OSError:
        pass
    try:  # macOS / BSD
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                             text=True, timeout=3).stdout
        for line in out.splitlines():
            if line.strip().startswith("gateway:"):
                return line.split()[1]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _resolve_targets(raw: list[str]) -> list[str]:
    """Expand the literal token 'gw' (or 'gateway'/'auto') to the node's detected default gateway;
    drop it if detection fails. Other entries (explicit IPs/hosts) pass through unchanged."""
    out = []
    for t in raw:
        if t.lower() in ("gw", "gateway", "auto"):
            gw = default_gateway()
            if gw and gw not in out:
                out.append(gw)
        elif t not in out:
            out.append(t)
    return out


# ping + net (fast loop). Default pings 1.1.1.1 + this node's own gateway (auto-detected), so a
# fresh install needs no per-site address. Set SMOKEMON_TARGETS to override; 'gw' = the gateway.
TARGETS = _resolve_targets(_list("SMOKEMON_TARGETS", "1.1.1.1,gw"))
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

# Redis stream/queue health. Auto by default: the probe runs every slow cycle and samples
# only if a Redis is reachable at REDIS_HOST:PORT (tiny RESP socket reads, no redis-cli).
# On a node with no Redis it is a silent no-op; set SMOKEMON_REDIS=1 to force a down row to
# be recorded when it is unreachable, or SMOKEMON_REDIS=0 to disable entirely. Streams are
# comma-separated; groups use stream=group pairs separated by semicolons.
REDIS_ENABLED = _enabled("SMOKEMON_REDIS", True)
REDIS_FORCED = _forced_on("SMOKEMON_REDIS")
REDIS_HOST = os.environ.get("SMOKEMON_REDIS_HOST", "127.0.0.1")
REDIS_PORT = _i("SMOKEMON_REDIS_PORT", "6379")
REDIS_TIMEOUT = _f("SMOKEMON_REDIS_TIMEOUT", "1")
REDIS_INTERVAL = _f("SMOKEMON_REDIS_INTERVAL", "60")
REDIS_STREAMS = _list("SMOKEMON_REDIS_STREAMS", "")
REDIS_GROUPS = _semi_list("SMOKEMON_REDIS_GROUPS", "")

# Docker container health. Auto by default: the probe runs every slow cycle but only
# samples when the docker socket exists, so nodes without docker are a silent no-op. When
# present it issues one bounded HTTP GET over the socket (stdlib socket + manual HTTP/1.0,
# no docker CLI, no `docker logs`, no log/journal tails). Optional per-container cpu/mem
# from cgroup v2 (/sys reads). DOCKER_INSPECT adds restart_count/exit_code/oom_killed via a
# small per-container inspect, capped at DOCKER_MAX. SMOKEMON_DOCKER=1 forces a daemon-down
# row even when the socket is absent; SMOKEMON_DOCKER=0 disables entirely.
DOCKER_ENABLED = _enabled("SMOKEMON_DOCKER", True)
DOCKER_FORCED = _forced_on("SMOKEMON_DOCKER")
DOCKER_SOCK = os.environ.get("SMOKEMON_DOCKER_SOCK", "/var/run/docker.sock")
DOCKER_API = os.environ.get("SMOKEMON_DOCKER_API", "v1.41")
DOCKER_INTERVAL = _f("SMOKEMON_DOCKER_INTERVAL", "60")
DOCKER_TIMEOUT = _f("SMOKEMON_DOCKER_TIMEOUT", "2")
DOCKER_MAX_BYTES = _i("SMOKEMON_DOCKER_MAX_BYTES", str(512 * 1024))
DOCKER_MAX = _i("SMOKEMON_DOCKER_MAX", "60")
DOCKER_INSPECT = os.environ.get("SMOKEMON_DOCKER_INSPECT", "1") != "0"
DOCKER_CGROUP = os.environ.get("SMOKEMON_DOCKER_CGROUP", "1") != "0"

# Pipeline / process liveness. On by default with zero config. PROC_WATCH matches
# substrings against /proc cmdlines and reports count/cpu/rss, the youngest process's
# uptime, and a cumulative restart count (flips when the youngest starttime changes).
# RTSP_URLS sends a bounded OPTIONS per endpoint to confirm a stream is actually served.
# When PIPELINE_AUTO is on (default) and a list is empty, the probe auto-detects: it watches
# any running gst-launch process and probes every rtsp:// URL found inside those cmdlines
# (e.g. rtspclientsink location=...). Pure stdlib, no ps/ffprobe, no log tails.
#   SMOKEMON_PIPELINE=0      disable entirely
#   SMOKEMON_PIPELINE_AUTO=0 only use the explicit lists below (no gst/rtsp auto-detection)
#   SMOKEMON_PROC_WATCH='gst=gst-launch-1.0;app=python app.py'   (label=substring; semis)
#   SMOKEMON_RTSP_URLS='cam=rtsp://127.0.0.1:8554/imx519'        (label=url, or bare url)
PIPELINE_ENABLED = _enabled("SMOKEMON_PIPELINE", True)
PIPELINE_AUTO = _enabled("SMOKEMON_PIPELINE_AUTO", True)
PROC_WATCH = _semi_list("SMOKEMON_PROC_WATCH", "")
RTSP_URLS = _semi_list("SMOKEMON_RTSP_URLS", "")
PIPELINE_INTERVAL = _f("SMOKEMON_PIPELINE_INTERVAL", "60")
RTSP_TIMEOUT = _f("SMOKEMON_RTSP_TIMEOUT", "2")

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
_gw = default_gateway()  # label the auto-detected gateway "gw" too, so renders read nicely
if _gw:
    TARGET_LABELS.setdefault(_gw, "gw")
HTTP_COLORS = ["cyan", "green+", "magenta+", "blue+", "orange+"]
PANELS = ["ping", "net", "http", "mtr", "wifi", "iperf",
          "host", "gpu", "redis", "docker", "pipeline", "disk",
          "thermal", "power", "tcp", "psi", "freq", "self"]
