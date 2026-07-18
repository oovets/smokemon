"""Central configuration: all SMOKEMON_* env vars, node identity and paths."""

import hashlib
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


def _enabled(name: str, default: bool) -> bool:
    """Tri-state on/off: unset -> default (auto). '0/false/no/off' -> disabled, else on."""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def cli_path(env_var: str, name: str) -> str:
    """Resolve a CLI: explicit env var -> PATH lookup -> bare name (resolved at exec)."""
    return os.environ.get(env_var) or shutil.which(name) or name


NODE = os.environ.get("SMOKEMON_NODE") or socket.gethostname()
DB_PATH = os.environ.get("SMOKEMON_DB", os.path.join(HOME, "smokemon", "data", "smokemon.db"))
HUB_DB = os.environ.get("SMOKEMON_HUB_DB", os.path.join(HOME, "smokemon", "data", "smokemon-hub.db"))
# Node config file install.sh writes (KEY=value lines; read by the systemd shipper). Used by
# `smoke hub` to show/repoint where this node ships. Overridable for tests / non-default layouts.
ENV_FILE = os.environ.get("SMOKEMON_ENV_FILE", "/etc/smokemon.env")

_gateway_cache: list[str | None] = []


def default_gateway() -> str | None:
    """The node's IPv4 default-gateway IP from /proc/net/route, stdlib-only. Lets every node
    ping its own first hop without hardcoding a per-site LAN address. Returns None if it can't
    be determined. Memoized: this is read at import time and the route does not change often
    enough to be worth re-reading on every call."""
    if _gateway_cache:
        return _gateway_cache[0]
    gw = None
    try:  # the default route is the line with destination 00000000
        with open("/proc/net/route") as f:
            for line in f.read().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 0x2:
                    h = fields[2]  # gateway, little-endian hex -> dotted quad
                    gw = ".".join(str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0))
                    break
    except OSError:
        pass
    _gateway_cache.append(gw)
    return gw


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

# wifi (slow loop)
PROBE_INTERVAL = _f("SMOKEMON_PROBE_INTERVAL", "60")
WIFI_ENABLED = os.environ.get("SMOKEMON_WIFI", "1") != "0"

# ---------------------------------------------------------------------------
# Incident detection. Normal operation is held in a bounded memory window and never
# written; only confirmed state transitions and the evidence around them reach disk.
# ---------------------------------------------------------------------------

# Signal registry. The memory ceiling is SIGNAL_MAX * SIGNAL_RING * 3 * 8 bytes (~74 KB at
# the defaults) and is enforced in signals.feed(), so a node churning container names or
# interface aliases cannot grow it. SIGNAL_RING is ~10x INCIDENT_PRE_SAMPLES so the ring
# also serves debounce evaluation without a second buffer.
SIGNAL_MAX = _i("SMOKEMON_SIGNAL_MAX", "48")
SIGNAL_RING = _i("SMOKEMON_SIGNAL_RING", "64")
# A signal silent this long with an incident open is closed as 'stale': a dead probe must not
# leave an incident open forever, since the hub cannot tell that from a real ongoing fault.
SIGNAL_STALE_S = _f("SMOKEMON_SIGNAL_STALE_S", "600")

# Per-node baseline (EWMA centre + mean absolute deviation). TAU is wall-clock, and the decay
# weight is derived from the actual dt between samples, so a 10 s signal and a 300 s signal
# learn at the same real-world rate.
BASELINE_TAU_S = _f("SMOKEMON_BASELINE_TAU_S", "86400")
BASELINE_GATE_Z = _f("SMOKEMON_BASELINE_GATE_Z", "4.0")   # winsorising threshold
BASELINE_MAX_N = _i("SMOKEMON_BASELINE_MAX_N", "100000")  # warmup counter saturation
# Flushed on a timer, not per sample: per-sample would be ~8600 extra commits/day and spend
# the whole SD-write budget on bookkeeping. Cost: a crash loses up to this much learning.
BASELINE_FLUSH_S = _f("SMOKEMON_BASELINE_FLUSH_S", "900")

# Sparse per-field rule overrides, e.g.
#   SMOKEMON_RULES='ping.loss:trip=15,for_s=30;host.temp:trip=75'
# so an operator never has to restate a whole rule to move one number.
RULES_SPEC = os.environ.get("SMOKEMON_RULES", "")

# Evidence retained per incident. Worst case is PRE + DURING_MAX + POST rows regardless of how
# long the incident lasts: the DURING ladder keeps native cadence for HEAD samples and then
# backs off exponentially, so a three-day outage costs about the same as a three-minute one.
INCIDENT_PRE_SAMPLES = _i("SMOKEMON_INCIDENT_PRE", "6")
INCIDENT_POST_SAMPLES = _i("SMOKEMON_INCIDENT_POST", "3")
INCIDENT_DURING_MAX = _i("SMOKEMON_INCIDENT_DURING_MAX", "24")
INCIDENT_DURING_HEAD = _i("SMOKEMON_INCIDENT_DURING_HEAD", "6")
INCIDENT_DURING_STEP0 = _f("SMOKEMON_INCIDENT_DURING_STEP0", "60")
INCIDENT_DURING_GROWTH = _f("SMOKEMON_INCIDENT_DURING_GROWTH", "2.0")
INCIDENT_DURING_STEP_MAX = _f("SMOKEMON_INCIDENT_DURING_STEP_MAX", "3600")
# Beyond this many concurrent incidents, new ones record transitions only. Degrade detail,
# never detection -- a node in a storm is exactly when we must not stop noticing things.
INCIDENT_MAX_OPEN = _i("SMOKEMON_INCIDENT_MAX_OPEN", "16")
# Force-close an incident that has been open this long. For RELATIVE rules the baseline then
# thaws and relearns the new regime; for ABSOLUTE safety rules it stays frozen and a
# 'persistent' transition is recorded instead, so expiry can never become a silent
# auto-acknowledge of a permanent fault.
INCIDENT_MAX_OPEN_S = _f("SMOKEMON_INCIDENT_MAX_OPEN_S", "86400")
# Whether a re-trip continues the same incident (same uid) or starts a new one. Distinct from
# a rule's cooldown_s: cooldown governs when the detector may trip again, this governs whether
# it counts as the same occurrence.
INCIDENT_REOPEN_WINDOW_S = _f("SMOKEMON_INCIDENT_REOPEN_WINDOW_S", "900")

# Run the detector for real -- rules, debounce, hysteresis, baseline -- but log what it WOULD
# have written instead of writing it. Bring-up aid: run a node for a day in this mode to see
# the real incident rate before committing to thresholds, and to test a threshold change
# before releasing it to the fleet. Nothing reaches disk, so it is safe to leave on.
DETECT_DRYRUN = os.environ.get("SMOKEMON_DETECT_DRYRUN", "0") != "0"

# Liveness + slow-trend summary. The only row a healthy node writes, and the reason the hub
# can tell "healthy" from "dead" once continuous sampling stops reaching disk.
HEARTBEAT_INTERVAL = _f("SMOKEMON_HEARTBEAT_INTERVAL", "300")

# host health
HOST_INTERVAL = _f("SMOKEMON_HOST_INTERVAL", "30")
# CPU temperature (degC) at which most SoCs begin thermal throttling. detect.RULES derives
# host.temp's trip/clear from it, so a node with a different SoC moves both by moving this one
# number rather than restating the rule. Pi default is ~80-85C.
THROTTLE_TEMP_C = _f("SMOKEMON_THROTTLE_TEMP", "80")

# push/webhook alerting (S4); set SMOKEMON_NOTIFY_URL to an ntfy topic, a Slack/Discord
# incoming webhook, or any URL that accepts a JSON {title, body} POST. Kind is
# auto-detected from the host. Only incidents at or above NOTIFY_MIN_SEVERITY fire.
NOTIFY_URL = os.environ.get("SMOKEMON_NOTIFY_URL", "")
NOTIFY_KIND = os.environ.get("SMOKEMON_NOTIFY_KIND", "")  # "" = auto-detect
NOTIFY_MIN_SEVERITY = _i("SMOKEMON_NOTIFY_MIN_SEVERITY", "2")

# Hub-side alert delivery (delivery-only). A background thread in the hub process projects the
# incidents the nodes have already opened (hubapi.open_incident_alerts) and pushes newly-firing
# and newly-resolved ones to SMOKEMON_NOTIFY_URL via notify.py. Detection is NOT repeated here:
# the node already did debounce, hysteresis, cooldown and dedup, and re-deciding hub-side would
# be a second, disagreeing opinion. This adds only delivery (flap-suppression / mute /
# re-notify cooldown). No-op unless NOTIFY_URL is set. Paging bar is NOTIFY_MIN_SEVERITY (1-3).
ALERT_EVAL_INTERVAL = _f("SMOKEMON_ALERT_EVAL_INTERVAL", "60")  # seconds between passes
ALERT_RENOTIFY_S = _f("SMOKEMON_ALERT_RENOTIFY_S", "1800")      # re-page a still-firing alert after this
ALERT_NOTIFY_RESOLVED = os.environ.get("SMOKEMON_ALERT_NOTIFY_RESOLVED", "1") != "0"
# opt-out: semicolon list of fnmatch globs matched against the alert key. A matched alert is
# never paged (it still shows in the dashboard). Since the pivot the key is the incident uid,
# so a glob targets an incident rather than a service name.
ALERT_MUTE = _semi_list("SMOKEMON_ALERT_MUTE", "")
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
# Short-TTL response cache for the expensive aggregate endpoints (risks/services/fleet/heatmap/
# cost). These recompute from scratch per request - risks loops every node x several loaders and
# reruns services - so every dashboard poll, tab, reload and user paid full cost. Serving a value
# up to this many seconds old makes repeat/concurrent loads instant. 0 disables the cache.
HUB_CACHE_TTL_S = _f("SMOKEMON_HUB_CACHE_TTL_S", "20")

# Retention / pruning of the node DB (run `python -m smokemon.prune`, e.g. from a daily timer).
# Rows older than RETENTION_DAYS are deleted, but only once they have been shipped (id <=
# ship_state cursor) when a hub is configured - so a long hub outage never loses data. With no
# hub, age alone applies. 0 disables pruning entirely. After deleting, the WAL is checkpoint-
# truncated so the file actually shrinks; freed main-DB pages are reused by later inserts.
# PRUNE_VACUUM=1 additionally runs a full VACUUM (heavy, needs free space) to reclaim pages.
RETENTION_DAYS = _f("SMOKEMON_RETENTION_DAYS", "14")
PRUNE_VACUUM = os.environ.get("SMOKEMON_PRUNE_VACUUM", "0") != "0"

# Footprint governor (node-side, opt-in). When this process exceeds a budget, the collector
# sheds the probes named in governor.EXPENSIVE for that cycle and logs a throttled governor
# event, so detail degrades gracefully instead of the footprint blowing past target.
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

# Map your target IPs to friendly names; used when labelling a signal's entity.
TARGET_LABELS = {"1.1.1.1": "internet", "192.168.0.1": "gw"}
_gw = default_gateway()  # label the auto-detected gateway "gw" too, so renders read nicely
if _gw:
    TARGET_LABELS.setdefault(_gw, "gw")
