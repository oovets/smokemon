"""Hub-side shaping of incidents the nodes already detected: correlation into clusters,
target classification and the robust statistics those need.

Detection itself moved to smokemon.detect on the node -- the continuous series it used to
run over is no longer stored, so nothing here re-derives incidents. What remains works on
the reconstructed incident list from query.load_incidents.

Guardrail: this module is hub-side and read-only. It is NEVER imported by collectors /
ship / probes / adapters. Pure stdlib so it also runs unchanged on a node when someone
wants a local report."""

import statistics

from . import config

# ---------- small statistics helpers (pure, list-based) ----------


def _finite(seq):
    """Drop None and NaN; return a plain list of floats."""
    return [float(v) for v in seq if v is not None and v == v]


def _median(seq):
    f = _finite(seq)
    return statistics.median(f) if f else None


def _mad(seq, center=None):
    """Median absolute deviation. Robust spread estimate that ignores outliers,
    which is exactly what an anomaly baseline wants. Returns None when empty."""
    f = _finite(seq)
    if not f:
        return None
    c = statistics.median(f) if center is None else center
    return statistics.median([abs(v - c) for v in f])


def robust_z(value, center, mad, tiny=1e-9):
    """How many robust-sigma `value` sits from `center`. Uses 1.4826*MAD (the MAD->
    stddev scale factor for a normal). When MAD is ~0 (a near-constant baseline) any
    real deviation is effectively infinite, so report a large finite z instead of
    dividing by zero. Returns 0.0 when value equals center."""
    if value is None or center is None or mad is None:
        return 0.0
    scale = 1.4826 * mad
    if scale < tiny:
        return 0.0 if abs(value - center) < tiny else (50.0 if value > center else -50.0)
    return (value - center) / scale


# ---------- ping target classification ----------

# Anything in these RFC1918 / CGNAT / loopback ranges is a local/gateway path, not
# the internet. Tailscale (100.64/10) is its own class so a tailnet blip is not
# misread as an ISP outage.
_PRIVATE_PREFIXES = ("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.2", "172.30.", "172.31.", "127.")


def classify_target(name: str) -> str:
    """'gw' (LAN/gateway), 'tailscale', or 'internet' for a ping target name/IP.
    Honours config.TARGET_LABELS first (an explicit 'gw'/'internet' label wins)."""
    label = config.TARGET_LABELS.get(name, "").lower()
    if "gw" in label or "gateway" in label or "lan" in label:
        return "gw"
    if "tailscale" in label or "tailnet" in label:
        return "tailscale"
    if name.startswith("100.") or "tailscale" in name.lower():
        return "tailscale"
    if name.startswith(_PRIVATE_PREFIXES):
        return "gw"
    return "internet"


def classify_targets(ping_data: dict) -> dict[str, list[str]]:
    """{class: [target names]} for every target name in an iterable of targets."""
    out: dict[str, list[str]] = {"internet": [], "gw": [], "tailscale": []}
    for name in ping_data:
        out[classify_target(name)].append(name)
    return out


# ---------- correlation + shared formatting ----------

# detect.py stores severity as the rule's word; ordering and the numeric paging bar
# (config.NOTIFY_MIN_SEVERITY, 1-3) both need a rank, so the mapping lives here rather than
# being re-guessed by every surface that sorts or gates on it.
SEVERITY_RANK = {"info": 0, "warn": 1, "error": 2, "crit": 3}


def severity_rank(severity: str | None) -> int:
    """Numeric rank for a stored severity word. Unknown words rank as 'warn' rather than 0 --
    a rule severity this build does not recognise must not be silently dropped below the
    paging bar."""
    return SEVERITY_RANK.get(severity or "", 1)


def correlate_incidents(incidents: list[dict], window_s: float = 120.0) -> list[dict]:
    """Group incidents that overlap (or sit within `window_s` of each other) in time into
    correlated clusters, so one root cause - a thermal throttle trips loss + latency + a
    docker restart at once - reads as a single incident instead of a storm of separate rows.

    Each group is {start, end, duration_s, severity, root, members} where `root` is the
    highest-severity member (tie-broken by earliest start) and `members` keeps every raw
    incident so a genuine second fault inside the window is never hidden. Pure stdlib; the
    grouping reuses the same gap-merge logic as merge_spans but carries the members along.
    Incidents need only {start, end, severity}, which the reconstructed incidents from
    query.load_incidents provide."""
    if not incidents:
        return []
    ordered = sorted(incidents, key=lambda i: i["start"])
    groups: list[list[dict]] = [[ordered[0]]]
    span_end = ordered[0]["end"]
    for inc in ordered[1:]:
        if inc["start"] <= span_end + window_s:
            groups[-1].append(inc)
            span_end = max(span_end, inc["end"])
        else:
            groups.append([inc])
            span_end = inc["end"]
    out: list[dict] = []
    for members in groups:
        root = max(members, key=lambda i: (i.get("severity", 1), -i["start"]))
        start = min(m["start"] for m in members)
        end = max(m["end"] for m in members)
        out.append({
            "start": start, "end": end, "duration_s": end - start,
            "severity": root.get("severity", 1), "root": root, "members": members,
        })
    return out


def merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Union of (start, end) intervals so overlapping per-target incidents are counted
    once. Returns disjoint spans sorted by start."""
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _dur(seconds: float) -> str:
    """Compact duration: '45s' / '6m' / '2h13m'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" if s == 0 else f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"
