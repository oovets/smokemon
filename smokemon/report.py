"""Text surfaces built on the analysis engine: a one-line sparkline status (QW3),
an incident report with multi-signal blame (F1/F2) and a plain-english daily digest
(F3). All stdlib and renderer-free, so they run on a node as well as the hub - no
plotext / matplotlib import."""

from datetime import datetime

from . import analyze, config, query

_SPARK = "▁▂▃▄▅▆▇█"
DIGEST_MAX_INCIDENTS = 10  # cap the digest's detail list; full list via `smoke incidents`


def sparkline(vals, lo=None, hi=None) -> str:
    """Unicode block sparkline of a numeric series. None/NaN render as a space so gaps
    stay visible. lo/hi can be pinned (e.g. 0..100 for a percentage) else autoscaled."""
    finite = [v for v in vals if v is not None and v == v]
    if not finite:
        return ""
    lo = min(finite) if lo is None else lo
    hi = max(finite) if hi is None else hi
    span = (hi - lo) or 1.0
    out = []
    for v in vals:
        if v is None or v != v:
            out.append(" ")
            continue
        frac = (v - lo) / span
        idx = max(0, min(len(_SPARK) - 1, int(frac * (len(_SPARK) - 1) + 0.5)))
        out.append(_SPARK[idx])
    return "".join(out)


def _spark_resampled(t, vals, since, until, width=16, agg="mean", lo=None, hi=None) -> str:
    """Resample (t, vals) to `width` buckets then sparkline it - a fixed-width glance
    regardless of how many raw samples the window holds."""
    if not t:
        return ""
    bucket = max(1.0, (until - since) / width)
    _, rs = analyze.resample(t, vals, since, until, bucket, agg)
    return sparkline(rs, lo, hi)


def _internet_target(ping: dict) -> str | None:
    """The ping target that represents the WAN path: prefer one classified 'internet'
    with the largest median RTT, else any target."""
    inet = [n for n in ping if analyze.classify_target(n) == "internet"] or list(ping)
    if not inet:
        return None
    return max(inet, key=lambda n: query.idle_rtt_ms({n: ping[n]}) or 0.0)


# ---------- QW3: sparkline status line ----------


def status_line(conn, since, until, node=None) -> str:
    """One glanceable row: internet RTT spark + current, wifi RSSI, cpu temp, verdict.
    e.g. 'internet ▁▂▅▇▃ 6ms loss0% · wifi ▆▅▄ -52dBm · cpu ▂▃ 45C · healthy'."""
    ping = query.load_ping_agg(conn, since, until, None, node)
    http = query.load_http(conn, since, until, node)
    parts = []

    tgt = _internet_target(ping)
    if tgt:
        d = ping[tgt]
        spark = _spark_resampled(d["t"], d["med"], since, until)
        cur = query.last_value(d["med"])
        avg_loss = sum(d["loss"]) / len(d["loss"]) if d["loss"] else 0.0
        cur_s = f"{cur:.0f}ms" if cur is not None else "--"
        parts.append(f"internet {spark} {cur_s} loss{avg_loss:.0f}%")

    wifi = query.load_wifi(conn, since, until, node)
    if wifi and any(r is not None for r in wifi.get("rssi", [])):
        spark = _spark_resampled(wifi["t"], wifi["rssi"], since, until)
        cur = query.last_value(wifi["rssi"])
        parts.append(f"wifi {spark} {cur:.0f}dBm" if cur is not None else f"wifi {spark}")

    host = query.load_host(conn, since, until, node)
    if host and any(c is not None for c in host.get("cpu", [])):
        spark = _spark_resampled(host["t"], host["cpu"], since, until, lo=0, hi=100)
        temp = query.last_value(host.get("temp", []))
        tag = f" {temp:.0f}C" if temp is not None else ""
        parts.append(f"cpu {spark}{tag}")

    parts.append(_verdict(ping, http, until))
    return " · ".join(parts)


def verdict(conn, since, until, node=None, recent_s: float = 300.0) -> str:
    """Public one-word health verdict for a window (used by the live bell, X2)."""
    ping = query.load_ping_agg(conn, since, until, None, node)
    http = query.load_http(conn, since, until, node)
    return _verdict(ping, http, until, recent_s)


def _verdict(ping: dict, http: dict, until: float, recent_s: float = 300.0) -> str:
    """Health word from incidents still open in the last `recent_s` of the window."""
    incidents = analyze.detect_incidents(ping, http)
    active = [i for i in incidents if i["end"] >= until - recent_s]
    if active:
        worst = max(active, key=lambda i: i["severity"])
        return worst["klass"].upper().replace("-", " ")
    if incidents:
        return "recovered"
    return "healthy"


# ---------- F1 + F2: incident report ----------


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def incidents_report(conn, since, until, node=None) -> str:
    """Incident table with per-incident multi-signal blame. Read-only."""
    ping = query.load_ping_agg(conn, since, until, None, node)
    http = query.load_http(conn, since, until, node)
    incidents = analyze.detect_incidents(ping, http)
    span_h = (until - since) / 3600
    head = f"{len(incidents)} incident(s) in the last {span_h:.1f}h"
    if node:
        head = f"[{node}] " + head
    if not incidents:
        return head + " — all clear."
    frame = analyze.build_frame(conn, since, until, node)
    lines = [head + ":", ""]
    for i in incidents:
        lines.append(f"[{_hhmm(i['start'])}-{_hhmm(i['end'])}] {i['klass']:<13} "
                     f"{i['scope']:<10} {i['detail']}")
        causes = analyze.explain_incident(frame, i["start"], i["end"], conn, node)
        if causes:
            lines.append("   └ correlates with: " + " · ".join(causes))
    return "\n".join(lines)


# ---------- F3: plain-english daily digest ----------


def digest(conn, since, until, node=None) -> str:
    """Narrative summary: uptime, blips, peak latency + what it coincided with,
    bufferbloat grade, wifi roams, thermals. Built on the F1/F2 engine."""
    ping = query.load_ping_agg(conn, since, until, None, node)
    http = query.load_http(conn, since, until, node)
    iperf = query.load_iperf(conn, since, until, node)
    wifi = query.load_wifi(conn, since, until, node)
    host = query.load_host(conn, since, until, node)

    span_h = (until - since) / 3600
    name = node or config.NODE
    title = (f"smokemon digest — {name} — "
             f"{datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → "
             f"{datetime.fromtimestamp(until):%H:%M}  ({span_h:.1f}h)")
    lines = [title, "=" * len(title), ""]

    # Uptime = internet samples reachable (loss < 100) / total.
    tgt = _internet_target(ping)
    if tgt:
        loss = ping[tgt]["loss"]
        total = len(loss)
        reachable = sum(1 for x in loss if (x or 0) < 100.0)
        up = 100.0 * reachable / total if total else 100.0
        lines.append(f"Uptime: {up:.2f}% internet-reachable ({config.TARGET_LABELS.get(tgt, tgt)}).")

    incidents = analyze.detect_incidents(ping, http)
    if incidents:
        by_class: dict[str, int] = {}
        for i in incidents:
            by_class[i["klass"]] = by_class.get(i["klass"], 0) + 1
        breakdown = ", ".join(f"{n} {k}" for k, n in sorted(by_class.items()))
        lines.append(f"{len(incidents)} incident(s): {breakdown}.")
        # Honest downtime = merged duration of full-outage incidents only (overlapping
        # per-target runs are unioned, never summed).
        outage = analyze.merge_spans([(i["start"], i["end"]) for i in incidents
                                      if i["klass"] in ("isp-outage", "link-down")])
        down_s = sum(e - s for s, e in outage)
        if down_s > 0:
            lines.append(f"Hard downtime: {analyze._dur(down_s)} across {len(outage)} outage(s).")
    else:
        lines.append("No incidents detected.")

    # Peak latency + coincidence.
    if tgt:
        meds = analyze._finite(ping[tgt]["med"])
        if meds:
            peak = max(meds)
            pidx = ping[tgt]["med"].index(peak)
            pts = ping[tgt]["t"][pidx]
            frame = analyze.build_frame(conn, since, until, node)
            causes = analyze.explain_incident(frame, pts, pts, conn, node)
            coin = f" (coincided with {causes[0]})" if causes else ""
            lines.append(f"Peak latency: {peak:.0f} ms at {_hhmm(pts)}{coin}.")

    bb = query.bufferbloat(iperf, ping)
    if bb:
        lines.append(f"Bufferbloat: grade {bb[0]} (+{bb[1]:.0f} ms under load).")

    if wifi and wifi.get("bssids_seen", 0) > 1:
        rssi_med = analyze._median(wifi.get("rssi", []))
        rssi_s = f", RSSI median {rssi_med:.0f} dBm" if rssi_med is not None else ""
        lines.append(f"WiFi: {wifi.get('roams', 0)} roam(s) across {wifi['bssids_seen']} APs{rssi_s}.")

    if host:
        temp = analyze._finite(host.get("temp", []))
        if temp:
            peak_t = max(temp)
            head = config.THROTTLE_TEMP_C - peak_t
            head_s = f"{head:.0f}C from throttle" if head > 0 else "THROTTLED"
            lines.append(f"Thermals: peak {peak_t:.0f}C ({head_s}).")

    if incidents:
        # Show the most significant first (severity, then longest); cap the wall of text.
        ranked = sorted(incidents, key=lambda i: (i["severity"], i["duration_s"]), reverse=True)
        top = ranked[:DIGEST_MAX_INCIDENTS]
        lines += ["", f"Top incidents (of {len(incidents)}):"]
        for i in top:
            lines.append(f"  - [{_hhmm(i['start'])}-{_hhmm(i['end'])}] {i['klass']} "
                         f"{i['scope']}: {i['detail']}")
        if len(incidents) > len(top):
            lines.append(f"  … {len(incidents) - len(top)} more (run `smoke incidents`).")
    return "\n".join(lines)
