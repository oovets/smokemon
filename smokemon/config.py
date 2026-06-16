"""Central configuration: all SMOKEMON_* env vars, node identity, paths, render constants."""

import hashlib
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
# Bearer token for token-authenticated destinations (incident.io HTTP alert sources). Sent as
# 'Authorization: Bearer <token>'. Only the incident_io kind uses it; ntfy/Slack/Discord ignore it.
NOTIFY_TOKEN = os.environ.get("SMOKEMON_NOTIFY_TOKEN", "")

# Hub-side service-alert delivery (delivery-only). A background thread in the hub process
# periodically re-evaluates the same fleet service/host degradations the Risk tab already shows
# (gst/watched-proc down, RTSP stream failing, docker restart-loops/unhealthy, redis/memory/
# throttle/conntrack - see hubapi._service_alerts) and pushes newly-firing and newly-resolved
# ones to SMOKEMON_NOTIFY_URL via notify.py. Detection is reused, not duplicated; this only adds
# delivery (dedup / flap-suppression / mute / re-notify cooldown). No-op unless NOTIFY_URL is set.
# The paging bar is the shared NOTIFY_MIN_SEVERITY (1-3).
ALERT_EVAL_INTERVAL = _f("SMOKEMON_ALERT_EVAL_INTERVAL", "60")  # seconds between passes
ALERT_WINDOW_HOURS = _f("SMOKEMON_ALERT_WINDOW_HOURS", "1")     # lookback defining "currently firing"
ALERT_RENOTIFY_S = _f("SMOKEMON_ALERT_RENOTIFY_S", "1800")      # re-page a still-firing alert after this
ALERT_NOTIFY_RESOLVED = os.environ.get("SMOKEMON_ALERT_NOTIFY_RESOLVED", "1") != "0"
# opt-out: semicolon list of fnmatch globs matched against the alert key "node/kind/label", e.g.
# 'pi04/*;*/docker/watchtower;*/*/scratch-*'. A matched alert is never paged (it still shows in
# the dashboard). kinds: docker / redis / stream / proc / memory / throttle / tcp.
ALERT_MUTE = _semi_list("SMOKEMON_ALERT_MUTE", "")
# opt-in allowlist (default-deny when set): semicolon list of fnmatch globs on the same
# "node/kind/label" key. When non-empty, an alert pages ONLY if it matches one of these (and is
# not muted) - everything else is tracked/shown but never sent. Empty = page everything that
# passes severity+mute (back-compat). Use this to page only "really down" + specced-service-down
# and keep utilization/warning kinds (memory/throttle/tcp/...) off the pager. Applied to paging
# only; alert_state and the dashboard still track every alert.
NOTIFY_ALLOW = _semi_list("SMOKEMON_NOTIFY_ALLOW", "")
# Run the hub's alert pass even with no webhook configured: it then only *tracks* firing alerts
# in alert_state (so the Risk tab shows "firing <duration>" and which would-be-paged), and sends
# nothing. Set =0 to disable the background pass entirely. Sending still requires NOTIFY_URL.
ALERT_TRACK = _enabled("SMOKEMON_ALERT_TRACK", True)

# central aggregation; set SMOKEMON_HUB_URL to the hub's /ingest endpoint
HUB_URL = os.environ.get("SMOKEMON_HUB_URL", "")
HUB_SECRET = os.environ.get("SMOKEMON_HUB_SECRET", "changeme")
# The shipper refuses to send (so the shared secret never crosses the wire in clear) unless
# HUB_URL is https, or the host is loopback, or this is set. Set =1 only for trusted LANs.
HUB_INSECURE = os.environ.get("SMOKEMON_HUB_INSECURE", "0") != "0"
SHIP_BATCH = _i("SMOKEMON_SHIP_BATCH", "2000")
SHIP_INTERVAL = _f("SMOKEMON_SHIP_INTERVAL", "0")  # 0 = drain once and exit
# Expedite: when an elevated ext_events row lands, the collector kicks an immediate ship so errors
# reach the hub in seconds rather than on the next bulk tick. Event-driven + rate-limited by the
# check interval (also the effective min gap between expedited ships). No-op without a hub.
SHIP_EXPEDITE = os.environ.get("SMOKEMON_SHIP_EXPEDITE", "1") != "0"
SHIP_EXPEDITE_INTERVAL = _f("SMOKEMON_SHIP_EXPEDITE_INTERVAL", "10")
# Raw per-ping rtts stay node-local by default: the hub renders percentile bands from the
# pre-aggregated rtt_min/p25/median/p75/max in ping_runs and never reads raw ping_rtts for
# fresh rows, so shipping them is ~85% of ship traffic for zero hub-side gain. Opt in if a
# hub-side consumer ever needs the raw distribution.
SHIP_RTTS = os.environ.get("SMOKEMON_SHIP_RTTS", "0") != "0"
# Tables to NOT ship to the hub. The rows are still collected and kept node-local; they are just
# excluded from the gather/push. Backward compatible - the hub simply receives fewer table keys
# and ignores the absence (UNIQUE(node,src_id) ingest never required any table).
#
# A small default set is excluded out of the box: tables the hub has no reader for, so shipping
# and storing them hub-side is pure dead weight. synthetic_samples (DoH/captive-portal checks) is
# written node-local but no hub surface queries it. SMOKEMON_SHIP_EXCLUDE (comma-separated) ADDS
# to this default rather than replacing it, so adding your own never silently re-enables a known
# dead-weight table. To force-ship a defaulted table, name it in SMOKEMON_SHIP_INCLUDE.
_SHIP_EXCLUDE_DEFAULT = frozenset({"synthetic_samples"})
SHIP_INCLUDE = frozenset(_list("SMOKEMON_SHIP_INCLUDE", ""))
SHIP_EXCLUDE = (_SHIP_EXCLUDE_DEFAULT | frozenset(_list("SMOKEMON_SHIP_EXCLUDE", ""))) - SHIP_INCLUDE
# proc_samples ship mode. The node records the top-N processes by cpu plus its own 'smokemon' row
# every host cycle (~30s); the hub only ever reads the 'smokemon' row (footprint panel + ship cost)
# and, during incident windows, the names/cpu of processes that were busy. So shipping every idle
# top-N row is mostly dead weight. Modes:
#   active (default) - always ship the 'smokemon' row; ship other proc rows only when their cpu_pct
#                      is >= SHIP_PROC_MIN_CPU (idle top-N rows stay node-local, kept for local
#                      `smoke incidents`). This is the new default: it trims the bulk of proc rows.
#   all              - ship every proc row (the prior behaviour; pick this if you attribute
#                      historical incidents to low-cpu processes hub-side).
#   self             - ship only the 'smokemon' row (most aggressive; drops hub-side proc
#                      attribution entirely).
SHIP_PROC = os.environ.get("SMOKEMON_SHIP_PROC", "active").strip().lower()
SHIP_PROC_MIN_CPU = _f("SMOKEMON_SHIP_PROC_MIN_CPU", "5.0")


def hub_dest(url: str) -> str:
    """Stable per-hub cursor key derived from the URL. If a hub's URL changes its dest changes
    too and that hub's ship_state cursor resets to 0 - the node then re-ships its un-pruned
    backlog to it, which is safe because the hub is idempotent on UNIQUE(node, src_id). Egress
    is wasted once, correctness never. Kept short so cursor rows stay compact."""
    return "h" + hashlib.sha1(url.encode()).hexdigest()[:12]


def _hubs() -> list[tuple[str, str]]:
    """Resolve the fan-out hub list as [(url, secret), ...]. Multiple hubs via the semicolon
    list SMOKEMON_HUB_URLS (URLs may contain commas); falls back to the single SMOKEMON_HUB_URL
    so existing single-hub setups are unchanged. Per-hub secret is optional and positional via
    SMOKEMON_HUB_SECRETS (an empty slot means "use the shared HUB_SECRET"); the common case sets
    nothing and every hub shares HUB_SECRET. URLs are de-duplicated preserving order."""
    urls = _semi_list("SMOKEMON_HUB_URLS", "") or ([HUB_URL] if HUB_URL else [])
    raw = os.environ.get("SMOKEMON_HUB_SECRETS", "")
    # split (not _semi_list) so empty positional slots survive and keep alignment with urls
    secrets = [s.strip() for s in raw.split(";")] if raw else []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, u in enumerate(urls):
        if u in seen:
            continue
        seen.add(u)
        sec = secrets[i] if i < len(secrets) and secrets[i] else HUB_SECRET
        out.append((u, sec))
    return out


HUBS = _hubs()  # [(url, secret), ...] - the fan-out destinations
HUB_BIND = os.environ.get("SMOKEMON_HUB_BIND", "0.0.0.0")
HUB_PORT = _i("SMOKEMON_HUB_PORT", "8765")
HUB_MAX_BODY = _i("SMOKEMON_HUB_MAX_BODY", str(64 * 1024 * 1024))
# latest-value endpoints (/api/latest, /metrics, /api/fleet-status) only need the most recent
# row per node/target. Bounding the lookup to a recent window keeps the MAX(ts) GROUP BY off the
# full history as the hub DB grows; a node silent longer than this drops out of "latest". 0 =
# unbounded (old behaviour). Default 30 days - generous enough to still show recently-dead nodes.
HUB_LATEST_WINDOW_S = _f("SMOKEMON_HUB_LATEST_WINDOW_S", str(30 * 86400))
# Short-TTL response cache for the expensive aggregate endpoints (risks/services/fleet/heatmap/
# cost). These recompute from scratch per request - risks loops every node x several loaders and
# reruns services - so every dashboard poll, tab, reload and user paid full cost. Serving a value
# up to this many seconds old makes repeat/concurrent loads instant. 0 disables the cache.
HUB_CACHE_TTL_S = _f("SMOKEMON_HUB_CACHE_TTL_S", "20")
# Data-transfer price ($/GB) applied to each node's measured ship volume (ingest_log.wire_bytes)
# to show an ingest cost per node on the dashboard. Hub-side only - no shipper/edge change. NOTE
# on AWS: data IN is free, so set this to your ACTUAL cost: 0 if ingress is free, ~0.045 if the
# hub sits behind a NAT Gateway (per-GB processing), ~0.09 for egress-priced transfer.
AWS_GB_COST = _f("SMOKEMON_AWS_GB_COST", "0.09")

# Retention / pruning of the node DB (run `python -m smokemon.prune`, e.g. from a daily timer).
# Rows older than RETENTION_DAYS are deleted, but only once they have been shipped (id <=
# ship_state cursor) when a hub is configured - so a long hub outage never loses data. With no
# hub, age alone applies. 0 disables pruning entirely. After deleting, the WAL is checkpoint-
# truncated so the file actually shrinks; freed main-DB pages are reused by later inserts.
# PRUNE_VACUUM=1 additionally runs a full VACUUM (heavy, needs free space) to reclaim pages.
RETENTION_DAYS = _f("SMOKEMON_RETENTION_DAYS", "14")
PRUNE_VACUUM = os.environ.get("SMOKEMON_PRUNE_VACUUM", "0") != "0"

# Footprint governor (node-side, opt-in). When this process exceeds a budget, the collector
# sheds its most expensive probes (mtr / synthetic / ext) for that cycle and logs a throttled
# governor event, so detail degrades gracefully instead of the footprint blowing past target.
# 0 = disabled (default), so out-of-the-box behaviour is unchanged.
MAX_RSS_MB = _f("SMOKEMON_MAX_RSS_MB", "0")     # this process's RSS ceiling (MB)
MAX_DB_MB = _f("SMOKEMON_MAX_DB_MB", "0")       # node DB (+WAL) size ceiling (MB)

# Device/environment inventory (delta-coded). Auto-on and cheap: one vslow scan emits a
# device_facts row only when a fact actually changes, so it captures model/kernel/OS/JetPack/
# versions/interfaces for ~zero steady-state cost. SMOKEMON_INVENTORY=0 disables.
INVENTORY_ENABLED = _enabled("SMOKEMON_INVENTORY", True)
INVENTORY_INTERVAL = _f("SMOKEMON_INVENTORY_INTERVAL", "3600")

# Event-driven log excerpts (opt-in, OFF by default). Ships a capped, redacted *tail* of the
# configured log files only when a warn/error+ event just landed in ext_events (governor sheds,
# probe anomalies) - never a stream (AGENTS.md forbids log streaming). A byte-offset cursor per
# file means bytes are never resent; each excerpt is hard-capped with drop-oldest (keep the
# freshest tail); secrets are redacted before storage; the shipper's gzip compresses the wire.
# SMOKEMON_LOGEXCERPT_ALWAYS=1 captures every cycle regardless of events (testing / manual pull).
LOGEXCERPT_ENABLED = _enabled("SMOKEMON_LOGEXCERPT", False)
LOGEXCERPT_PATHS = _list("SMOKEMON_LOGEXCERPT_PATHS", "")  # files to tail, comma-separated
LOGEXCERPT_INTERVAL = _f("SMOKEMON_LOGEXCERPT_INTERVAL", "60")
LOGEXCERPT_MAX_BYTES = _i("SMOKEMON_LOGEXCERPT_MAX_BYTES", str(16 * 1024))  # per-excerpt hard cap
LOGEXCERPT_ALWAYS = os.environ.get("SMOKEMON_LOGEXCERPT_ALWAYS", "0") != "0"

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
