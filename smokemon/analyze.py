"""Multi-signal analysis: incident detection, correlation/blame, statistical
baselines, mtr path intelligence and bandwidth attribution.

Roadmap F1 (blame engine), F2 (incident detection), P1 (time-of-day anomaly
baseline), P2 (change-point detection), P3 (mtr path intelligence) and X5
(bandwidth attribution) all live here.

Guardrail: this module is hub-side and read-only. It is NEVER imported by
collectors / ship / probes / adapters - it only reads an already-populated DB via
the shared smokemon.query loaders and derives everything from real collected
metrics (no fabricated / seeded values). Pure stdlib so it also runs unchanged on
a node when someone wants a local report."""

import statistics
from datetime import datetime

from . import config, query

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


def pearson(xs, ys):
    """Pearson correlation over index-aligned pairs where both values are finite.
    Returns None when fewer than 3 usable pairs or either side is constant."""
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if x is not None and y is not None and x == x and y == y]
    if len(pairs) < 3:
        return None
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    return sxy / (sxx ** 0.5 * syy ** 0.5)


# ---------- resampling onto a common timeline ----------


def resample(t, vals, since, until, bucket, agg="mean"):
    """Bucket an irregular (t, vals) series onto a fixed grid from `since` to `until`
    in steps of `bucket` seconds. Each grid point is the agg of the samples that fall
    in [b, b+bucket); empty buckets are None so series of different cadences (ping 10s,
    host 30s, http 60s) line up index-for-index. agg: mean | max | last | sum."""
    n = max(1, int((until - since) // bucket) + 1)
    buckets = [[] for _ in range(n)]
    for ti, v in zip(t, vals):
        if ti is None or v is None or v != v:
            continue
        idx = int((ti - since) // bucket)
        if 0 <= idx < n:
            buckets[idx].append(float(v))
    out = []
    for b in buckets:
        if not b:
            out.append(None)
        elif agg == "max":
            out.append(max(b))
        elif agg == "sum":
            out.append(sum(b))
        elif agg == "last":
            out.append(b[-1])
        else:
            out.append(sum(b) / len(b))
    grid = [since + i * bucket for i in range(n)]
    return grid, out


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
    """{class: [target names]} for every target present in a load_ping_agg dict."""
    out: dict[str, list[str]] = {"internet": [], "gw": [], "tailscale": []}
    for name in ping_data:
        out[classify_target(name)].append(name)
    return out


# ---------- F2: incident detection ----------

# An incident is a dict: {start, end, duration_s, klass, scope, detail, severity}.
# Severity is a coarse 1..3 rank used only for ordering / digest emphasis.

LOSS_FLOOR_PCT = 1.0        # below this a "loss" sample is just jitter noise
LOSS_INCIDENT_PEAK = 10.0   # a non-total loss run must peak at least this high to count
                            # (single 5%-of-20-packets blips are noise, not incidents)
MIN_RUN_CYCLES = 2          # contiguous bad samples needed to open an incident
SPIKE_FACTOR = 3.0          # median RTT this many times over baseline = a spike
SPIKE_FLOOR_MS = 30.0       # ...and at least this far above baseline, absolute
DNS_SLOW_MS = 80.0          # DNS phase above this with a fast TCP connect = resolver fault


def _runs(flags: list[bool]):
    """Yield (lo, hi) index pairs for each maximal run of True in `flags` (hi inclusive)."""
    lo = None
    for i, f in enumerate(flags):
        if f and lo is None:
            lo = i
        elif not f and lo is not None:
            yield lo, i - 1
            lo = None
    if lo is not None:
        yield lo, len(flags) - 1


def _loss_present(ping_data: dict, names: list[str], lo_ts: float, hi_ts: float) -> bool:
    """True if any of `names` shows loss above the floor anywhere in [lo_ts, hi_ts]."""
    for name in names:
        d = ping_data.get(name)
        if not d:
            continue
        for ts, loss in zip(d["t"], d["loss"]):
            if lo_ts <= ts <= hi_ts and (loss or 0) > LOSS_FLOOR_PCT:
                return True
    return False


def _detect_loss(ping_data: dict, cls: dict) -> list[dict]:
    incidents = []
    gw_names = cls["gw"]
    for name, d in ping_data.items():
        loss = d["loss"]
        flags = [(x or 0) > LOSS_FLOOR_PCT for x in loss]
        for lo, hi in _runs(flags):
            if hi - lo + 1 < MIN_RUN_CYCLES:
                continue
            window = loss[lo:hi + 1]
            peak = max(window)
            total_down = peak >= 99.5
            # Drop minor transient loss (a stray dropped packet or two). Sustained-but-low
            # loss still clears the bar once it has run >= MIN_RUN_CYCLES at >= the peak.
            if not total_down and peak < LOSS_INCIDENT_PEAK:
                continue
            start, end = d["t"][lo], d["t"][hi]
            kind = classify_target(name)
            # An internet path losing while the gateway is clean = upstream/ISP, not local.
            if kind == "internet" and gw_names and not _loss_present(ping_data, gw_names, start, end):
                klass = "isp-outage" if total_down else "upstream-loss"
            elif total_down:
                klass = "link-down"
            else:
                klass = "packet-loss"
            incidents.append({
                "start": start, "end": end, "duration_s": end - start,
                "klass": klass, "scope": config.TARGET_LABELS.get(name, name),
                "detail": f"loss peaked {peak:.0f}% over {_dur(end - start)}",
                "severity": 3 if total_down else 2,
            })
    return incidents


def _detect_latency(ping_data: dict) -> list[dict]:
    incidents = []
    for name, d in ping_data.items():
        med = d["med"]
        base = _median(med)
        if base is None:
            continue
        thresh = max(base * SPIKE_FACTOR, base + SPIKE_FLOOR_MS)
        flags = [m is not None and m == m and m > thresh for m in med]
        for lo, hi in _runs(flags):
            if hi - lo + 1 < MIN_RUN_CYCLES:
                continue
            window = _finite(med[lo:hi + 1])
            if not window:
                continue
            peak = max(window)
            start, end = d["t"][lo], d["t"][hi]
            incidents.append({
                "start": start, "end": end, "duration_s": end - start,
                "klass": "latency-spike", "scope": config.TARGET_LABELS.get(name, name),
                "detail": f"median RTT {peak:.0f} ms vs ~{base:.0f} ms baseline",
                "severity": 2 if peak > base * 5 else 1,
            })
    return incidents


def _detect_dns(http_data: dict) -> list[dict]:
    """dns-slow-but-tcp-fast: the resolver, not the link. One incident per URL window
    where the DNS phase dominates and the TCP connect is comparatively quick."""
    incidents = []
    for url, d in http_data.items():
        flags = []
        for i in range(len(d["t"])):
            ph = query.http_phases(d["dns"][i], d["connect"][i], d["tls"][i], d["ttfb"][i])
            tcp = ph["connect"]
            flags.append(ph["dns"] > DNS_SLOW_MS and ph["dns"] > 2 * max(tcp, 1.0))
        for lo, hi in _runs(flags):
            if hi - lo + 1 < MIN_RUN_CYCLES:
                continue
            peak = max(query.http_phases(d["dns"][i], d["connect"][i], d["tls"][i], d["ttfb"][i])["dns"]
                       for i in range(lo, hi + 1))
            start, end = d["t"][lo], d["t"][hi]
            incidents.append({
                "start": start, "end": end, "duration_s": end - start,
                "klass": "dns-slow", "scope": query.host_label(url),
                "detail": f"DNS {peak:.0f} ms while TCP connect stayed fast",
                "severity": 1,
            })
    return incidents


def detect_incidents(ping_data: dict, http_data: dict | None = None) -> list[dict]:
    """All incidents across loss, latency and DNS detectors, sorted by start time."""
    cls = classify_targets(ping_data)
    out = _detect_loss(ping_data, cls) + _detect_latency(ping_data)
    if http_data:
        out += _detect_dns(http_data)
    out.sort(key=lambda i: i["start"])
    return out


# ---------- F1: multi-signal correlation / blame ----------

# (frame series key, human label, "+" if higher-is-worse else "-", unit)
_BLAME_SIGNALS = [
    ("cpu", "cpu", "+", "%"), ("mem", "mem", "+", "%"), ("swap", "swap", "+", "%"),
    ("temp", "temp", "+", "C"), ("psi_cpu", "psi-cpu", "+", ""), ("psi_io", "psi-io", "+", ""),
    ("rssi", "wifi rssi", "-", "dBm"), ("retry_rate", "wifi retry", "+", "/s"),
    ("retrans", "tcp retrans", "+", "/s"), ("bw_in", "download", "+", "Mb/s"),
    ("bw_out", "upload", "+", "Mb/s"), ("mhz", "cpu clock", "-", "MHz"),
]

BLAME_Z = 3.0  # robust-sigma deviation in-window vs baseline to call a signal a suspect


def explain_incident(frame: dict, start: float, end: float, conn=None, node=None) -> list[str]:
    """F1 blame: for an incident window, name every signal that was anomalous relative
    to its own out-of-window baseline, plus any process that appeared during it. Ranked
    by deviation magnitude. Returns human-readable strings; empty when nothing stands out."""
    grid = frame["t"]
    in_idx = [i for i, g in enumerate(grid) if start - 1e-6 <= g <= end + frame["bucket"]]
    out_idx = [i for i in range(len(grid)) if i not in set(in_idx)]
    if not in_idx:
        return []
    suspects = []
    for key, label, direction, unit in _BLAME_SIGNALS:
        series = frame["series"].get(key)
        if not series:
            continue
        inv = _finite([series[i] for i in in_idx])
        outv = [series[i] for i in out_idx]
        if not inv:
            continue
        center = _median(outv)
        mad = _mad(outv, center)
        if center is None:
            continue
        in_mean = sum(inv) / len(inv)
        z = robust_z(in_mean, center, mad)
        score = z if direction == "+" else -z
        if score >= BLAME_Z:
            arrow = "up" if direction == "+" else "down"
            suspects.append((score, f"{label} {in_mean:.0f}{unit} ({arrow} from ~{center:.0f}{unit}, {score:.1f}sigma)"))
    suspects.sort(reverse=True)
    causes = [s for _, s in suspects]
    if conn is not None:
        procs = new_processes(conn, start, end, node)
        if procs:
            causes.append("new process: " + ", ".join(procs))
    return causes


def new_processes(conn, start: float, end: float, node=None, baseline_s: float = 1800.0) -> list[str]:
    """Process names present in proc_samples during [start,end] but absent in the
    `baseline_s` seconds immediately before it. The top-N proc sampler only keeps the
    heavy hitters, so a name appearing here is a process that climbed into the top set
    exactly when things went bad (X5 / F1 input). Read-only."""
    nf, npar = query._filt(node)
    inside = {r[0] for r in query._q(
        conn, "SELECT DISTINCT name FROM proc_samples WHERE ts BETWEEN ? AND ?" + nf,
        [start, end, *npar]) if r[0]}
    before = {r[0] for r in query._q(
        conn, "SELECT DISTINCT name FROM proc_samples WHERE ts BETWEEN ? AND ?" + nf,
        [start - baseline_s, start, *npar]) if r[0]}
    return sorted(inside - before)


# ---------- the analysis frame ----------

DEFAULT_BUCKET = 60.0


def build_frame(conn, since, until, node=None, bucket=DEFAULT_BUCKET) -> dict:
    """Resample every signal smokemon collects onto one common `bucket` grid so the
    correlation/anomaly code can treat them as index-aligned columns. Returns
    {'t', 'bucket', 'series': {name: [...]}, 'ping': raw, 'http': raw, 'mtr': raw}.
    All loads are read-only; missing tables simply yield empty series."""
    ping = query.load_ping_agg(conn, since, until, None, node)
    http = query.load_http(conn, since, until, node)
    host = query.load_host(conn, since, until, node)
    psi = query.load_psi(conn, since, until, node)
    wifi = query.load_wifi(conn, since, until, node)
    net = query.load_net(conn, since, until, node)
    tcp = query.load_tcp(conn, since, until, node)
    freq = query.load_freq(conn, since, until, node)
    mtr = query.load_mtr(conn, since, until, node)

    def rs(t, v, agg="mean"):
        return resample(t, v, since, until, bucket, agg)[1] if t else []

    cls = classify_targets(ping)
    internet = cls["internet"] or list(ping)
    inet_loss_t, inet_loss_v = [], []
    inet_rtt_t, inet_rtt_v = [], []
    for name in internet:
        d = ping.get(name, {})
        inet_loss_t += d.get("t", []); inet_loss_v += d.get("loss", [])
        inet_rtt_t += d.get("t", []); inet_rtt_v += d.get("med", [])

    # Bandwidth summed across real interfaces (lo/virtual already filtered by load_net).
    bw_in_t, bw_in_v, bw_out_t, bw_out_v = [], [], [], []
    for s in net.values():
        bw_in_t += s["t"]; bw_in_v += s["in"]
        bw_out_t += s["t"]; bw_out_v += s["out"]

    series = {
        "loss": rs(inet_loss_t, inet_loss_v, "max"),
        "rtt": rs(inet_rtt_t, inet_rtt_v, "mean"),
        "cpu": rs(host.get("t", []), host.get("cpu", [])),
        "mem": rs(host.get("t", []), host.get("mem", [])),
        "swap": rs(host.get("t", []), host.get("swap", [])),
        "temp": rs(host.get("t", []), host.get("temp", [])),
        "psi_cpu": rs(psi.get("t", []), psi.get("cpu", [])),
        "psi_io": rs(psi.get("t", []), psi.get("io", [])),
        "rssi": rs(wifi.get("t", []), wifi.get("rssi", [])),
        "retry_rate": rs(wifi.get("t", []), wifi.get("retry_rate", [])),
        "retrans": rs(tcp.get("t", []), tcp.get("retrans", [])),
        "bw_in": rs(bw_in_t, bw_in_v, "max"),
        "bw_out": rs(bw_out_t, bw_out_v, "max"),
        "mhz": rs(freq.get("t", []), freq.get("mhz", [])),
    }
    grid = [since + i * bucket for i in range(len(series["loss"]))] if series["loss"] else \
        [since + i * bucket for i in range(max(1, int((until - since) // bucket) + 1))]
    return {"t": grid, "bucket": bucket, "series": series,
            "ping": ping, "http": http, "mtr": mtr, "wifi": wifi}


# ---------- P1: time-of-day anomaly baseline ----------


def tod_key(ts: float) -> tuple[int, int]:
    """(weekday 0-6, hour 0-23) bucket for a timestamp. The baseline is per-bucket so
    'slow for a Tuesday 14:00' is judged against other Tuesday 14:00s, not the daily mean."""
    dt = datetime.fromtimestamp(ts)
    return dt.weekday(), dt.hour


def tod_baseline(t, vals) -> dict[tuple[int, int], tuple[float, float]]:
    """{(weekday,hour): (median, mad)} built from history. Buckets with <3 samples are
    skipped (too little to judge against)."""
    groups: dict[tuple[int, int], list[float]] = {}
    for ts, v in zip(t, vals):
        if v is None or v != v:
            continue
        groups.setdefault(tod_key(ts), []).append(float(v))
    out = {}
    for k, g in groups.items():
        if len(g) >= 3:
            c = statistics.median(g)
            out[k] = (c, statistics.median([abs(x - c) for x in g]))
    return out


def tod_anomalies(t, vals, z_thresh: float = 4.0, baseline=None) -> list[dict]:
    """Points whose value is > z_thresh robust-sigma above the median for their
    (weekday,hour) bucket. baseline can be passed in (e.g. a longer history) or is
    computed from the series itself. Returns [{ts, value, expected, z}]."""
    base = baseline if baseline is not None else tod_baseline(t, vals)
    out = []
    for ts, v in zip(t, vals):
        if v is None or v != v:
            continue
        bc = base.get(tod_key(ts))
        if not bc:
            continue
        center, mad = bc
        z = robust_z(float(v), center, mad)
        if z >= z_thresh:
            out.append({"ts": ts, "value": float(v), "expected": center, "z": z})
    return out


# ---------- P2: change-point / regime-shift detection ----------


def change_points(t, vals, min_seg: int = 5, min_shift_ratio: float = 0.4) -> list[dict]:
    """Detect sustained mean shifts (regime changes) via a single-pass recursive split
    on the largest before/after median gap. Catches silent permanent changes - an ISP
    speed-tier drop, a route change - that thresholds and spikes both miss. Returns
    [{ts, before, after, ratio}] sorted by time. min_seg guards against splitting on
    noise; min_shift_ratio is the |after-before|/max(|before|,eps) needed to report."""
    pts = [(ti, float(v)) for ti, v in zip(t, vals) if v is not None and v == v]
    out: list[dict] = []

    def split(lo, hi):
        if hi - lo + 1 < 2 * min_seg:
            return
        # Pick the boundary that best separates the two segments' means, weighted by
        # segment sizes (nL*nR/n * (meanL-meanR)^2). This peaks at the true regime
        # boundary; a plain median-gap scan ties along the whole flat run and would
        # report the earliest tie instead of the change point.
        best_i, best_score, best_lr = None, 0.0, None
        for i in range(lo + min_seg, hi - min_seg + 2):
            left_vals = [p[1] for p in pts[lo:i]]
            right_vals = [p[1] for p in pts[i:hi + 1]]
            nl, nr = len(left_vals), len(right_vals)
            ml, mr = sum(left_vals) / nl, sum(right_vals) / nr
            score = nl * nr / (nl + nr) * (ml - mr) ** 2
            if score > best_score:
                best_score, best_i = score, i
                best_lr = (statistics.median(left_vals), statistics.median(right_vals))
        if best_i is None:
            return
        left, right = best_lr
        ratio = abs(right - left) / max(abs(left), 1e-9)
        if ratio < min_shift_ratio:
            return
        out.append({"ts": pts[best_i][0], "before": left, "after": right, "ratio": ratio})
        split(lo, best_i - 1)
        split(best_i, hi)

    if len(pts) >= 2 * min_seg:
        split(0, len(pts) - 1)
    out.sort(key=lambda c: c["ts"])
    return out


# ---------- P3: mtr path intelligence ----------


def path_analysis(conn, since, until, node=None) -> dict[str, dict]:
    """Per-target mtr intelligence, read directly from mtr_hops (load_mtr collapses the
    per-sample host so it cannot see route churn). For each target: route changes (a
    hop_no whose resolved host changed between consecutive samples), the worst hop
    (largest mean loss, tie-broken by the latency it adds over the previous hop) and a
    stability score (fraction of hops whose host never changed across the window)."""
    nf, npar = query._filt(node)
    rows = query._q(conn,
                    "SELECT target, ts, hop_no, host, loss_pct, avg_ms FROM mtr_hops "
                    "WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY target, hop_no, ts",
                    [since, until, *npar])
    # per target -> per hop -> ordered samples
    by_target: dict[str, dict[int, list]] = {}
    for target, ts, hop_no, host, loss, avg in rows:
        by_target.setdefault(target, {}).setdefault(hop_no, []).append((ts, host, loss, avg))

    out: dict[str, dict] = {}
    for target, hops in by_target.items():
        changes, stable, worst = [], 0, None
        prev_avg = 0.0
        for hop_no in sorted(hops):
            samples = hops[hop_no]
            hosts_seq = [h for _, h, _, _ in samples if h]
            distinct = list(dict.fromkeys(hosts_seq))  # order-preserving unique
            if len(distinct) <= 1:
                stable += 1
            else:
                changes.append({"hop_no": hop_no, "from": distinct[0], "to": distinct[-1],
                                "seen": distinct})
            loss = _median([s[2] for s in samples]) or 0.0
            avg = _median([s[3] for s in samples]) or 0.0
            added = max(0.0, avg - prev_avg)
            prev_avg = avg
            cand = (loss, added, hop_no, distinct[-1] if distinct else None)
            if worst is None or cand[:2] > worst[:2]:
                worst = cand
        n = len(hops) or 1
        out[target] = {
            "hops": n,
            "stability": round(stable / n, 3),
            "route_changes": changes,
            "worst_hop": None if worst is None else {
                "hop_no": worst[2], "host": worst[3],
                "loss": round(worst[0], 1), "added_ms": round(worst[1], 1)},
        }
    return out


# ---------- X5: bandwidth attribution ----------


def bandwidth_attribution(conn, since, until, node=None, bucket=DEFAULT_BUCKET,
                          spike_z=3.0) -> list[dict]:
    """Heuristic "what's hammering my network": find bandwidth spikes (robust-z over the
    window), then name the processes whose cpu was highest in proc_samples during each
    spike bucket. proc_samples carries no per-process byte counters, so this is a
    coincidence ranking (cpu-at-spike-time), explicitly heuristic - never presented as
    a measured per-process byte count. Returns [{ts, mbps, direction, procs}]."""
    net = query.load_net(conn, since, until, node)
    if not net:
        return []
    bw_in_t, bw_in_v, bw_out_t, bw_out_v = [], [], [], []
    for s in net.values():
        bw_in_t += s["t"]; bw_in_v += s["in"]
        bw_out_t += s["t"]; bw_out_v += s["out"]
    out = []
    for label, (t, v) in (("down", (bw_in_t, bw_in_v)), ("up", (bw_out_t, bw_out_v))):
        grid, vals = resample(t, v, since, until, bucket, "max")
        finite = _finite(vals)
        if len(finite) < 5:
            continue
        center = statistics.median(finite)
        mad = _mad(finite, center)
        for g, val in zip(grid, vals):
            if val is None:
                continue
            if robust_z(val, center, mad) >= spike_z and val > 1.0:
                procs = _top_procs(conn, g, g + bucket, node)
                out.append({"ts": g, "mbps": round(val, 1), "direction": label, "procs": procs})
    out.sort(key=lambda x: x["mbps"], reverse=True)
    return out


def _top_procs(conn, lo, hi, node=None, limit=3) -> list[str]:
    """Names of the highest-cpu processes sampled in [lo, hi]."""
    nf, npar = query._filt(node)
    rows = query._q(conn,
                    "SELECT name, MAX(cpu_pct) c FROM proc_samples WHERE ts BETWEEN ? AND ?" + nf
                    + " GROUP BY name ORDER BY c DESC LIMIT ?", [lo, hi, *npar, limit])
    return [r[0] for r in rows if r[0]]


# ---------- shared formatting ----------


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
