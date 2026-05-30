"""Text surfaces built on the analysis engine: a one-line sparkline status (QW3),
an incident report with multi-signal blame (F1/F2) and a plain-english daily digest
(F3). All stdlib and renderer-free, so they run on a node as well as the hub - no
plotext / matplotlib import."""

import os
import sys
from datetime import datetime

from . import analyze, config, query

_SPARK = "▁▂▃▄▅▆▇█"
_SPARK_ASCII = ".:-=+*#@"   # ascii magnitude ramp for terminals that can't render the blocks
DIGEST_MAX_INCIDENTS = 10   # cap the digest's detail list; full list via `smoke incidents`

# Terminal-capability flags. The CLI flips ASCII on when stdout can't render unicode;
# tests and library callers keep the default (unicode, colour decided per-call). Same
# module-global pattern the tui renderer uses for KIOSK.
ASCII = False


def unicode_ok(stream=None) -> bool:
    """True when the stream is UTF-8 (so block sparklines / state dots render)."""
    enc = getattr(stream or sys.stdout, "encoding", "") or ""
    return "utf" in enc.lower()


def use_color(stream=None, *, disable: bool = False) -> bool:
    """True when ANSI colour is appropriate: a tty, NO_COLOR unset, not explicitly off.
    Follows the no-color.org convention - NO_COLOR present (any value) -> never colour."""
    if disable or "NO_COLOR" in os.environ:
        return False
    return bool(getattr(stream or sys.stdout, "isatty", lambda: False)())


def _sgr(s: str, code: str, color: bool) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if color else s


def _ramp() -> str:
    return _SPARK_ASCII if ASCII else _SPARK


def sparkline(vals, lo=None, hi=None) -> str:
    """Unicode block sparkline of a numeric series. None/NaN render as a space so gaps
    stay visible. lo/hi can be pinned (e.g. 0..100 for a percentage) else autoscaled."""
    finite = [v for v in vals if v is not None and v == v]
    if not finite:
        return ""
    lo = min(finite) if lo is None else lo
    hi = max(finite) if hi is None else hi
    span = (hi - lo) or 1.0
    chars = _ramp()
    out = []
    for v in vals:
        if v is None or v != v:
            out.append(" ")
            continue
        frac = (v - lo) / span
        idx = max(0, min(len(chars) - 1, int(frac * (len(chars) - 1) + 0.5)))
        out.append(chars[idx])
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


def _color_verdict(word: str, color: bool) -> str:
    """Tint the health verdict: green healthy, yellow recovered, red anything still open."""
    return _sgr(word, {"healthy": "32", "recovered": "33"}.get(word, "31"), color)


def status_line(conn, since, until, node=None, *, color: bool = False) -> str:
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

    gpu = _gpu_summary(conn, since, until, node)
    if gpu:
        parts.append(gpu)

    redis = _redis_summary(conn, since, until, node)
    if redis:
        parts.append(redis)

    ext = _ext_summary(conn, since, until, node)
    if ext:
        parts.append(ext)

    parts.append(_color_verdict(_verdict(ping, http, until), color))
    return " · ".join(parts)


def _gpu_summary(conn, since, until, node=None) -> str:
    gpu = query.load_gpu(conn, since, until, node)
    if not gpu:
        return ""
    vals = []
    for d in gpu.values():
        cur = query.last_value(d.get("util", []))
        if cur is not None:
            vals.append(cur)
    if not vals:
        return ""
    return f"gpu {max(vals):.0f}%"


def _redis_summary(conn, since, until, node=None) -> str:
    data = query.load_redis_latest(conn, since, until, node)
    if not data:
        return ""
    bad = any((inst.get("connected") or 0) < 1 for inst in data.values())
    if bad:
        return "redis down"
    max_pending = 0
    max_xlen = 0
    for inst in data.values():
        for stream in inst.get("streams", {}).values():
            max_xlen = max(max_xlen, stream.get("xlen") or 0)
            max_pending = max(max_pending, stream.get("pending") or 0)
    tail = f" pending{max_pending}" if max_pending else f" xlen{max_xlen}" if max_xlen else " ok"
    return "redis" + tail


def _ext_summary(conn, since, until, node=None) -> str:
    """Compact latest external health: 'ext 2/3 ok' or 'ext app down'."""
    latest = query.load_ext_latest(conn, since, until, node)
    if not latest:
        return ""
    down = []
    ok = 0
    for source, metrics in sorted(latest.items()):
        up = metrics.get("up", {}).get("value")
        if up is None:
            continue
        if up >= 1.0:
            ok += 1
        else:
            down.append(source)
    total = ok + len(down)
    if not total:
        return ""
    if down:
        shown = ",".join(down[:2])
        more = f"+{len(down) - 2}" if len(down) > 2 else ""
        return f"ext {shown}{more} down"
    return f"ext {ok}/{total} ok"


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


def incidents_report(conn, since, until, node=None, *, color: bool = False) -> str:
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
        klass = _sgr(f"{i['klass']:<13}", "31" if i.get("severity", 1) >= 2 else "33", color)
        lines.append(f"[{_hhmm(i['start'])}-{_hhmm(i['end'])}] {klass} "
                     f"{i['scope']:<10} {i['detail']}")
        causes = analyze.explain_incident(frame, i["start"], i["end"], conn, node)
        ext = query.load_ext_events(conn, i["start"], i["end"], node, limit=3)
        if ext:
            causes = [*causes, *[f"{e['source']} {e['event']}" for e in ext]]
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

    gpu = query.load_gpu(conn, since, until, node)
    gpu_peaks = [max(analyze._finite(d.get("util", []))) for d in gpu.values() if analyze._finite(d.get("util", []))]
    if gpu_peaks:
        lines.append(f"GPU: peak {max(gpu_peaks):.0f}%.")

    redis = query.load_redis_latest(conn, since, until, node)
    if redis:
        down = [inst for inst, d in redis.items() if (d.get("connected") or 0) < 1]
        if down:
            lines.append(f"Redis: down ({', '.join(down)}).")
        else:
            streams = []
            for d in redis.values():
                streams.extend(d.get("streams", {}).items())
            if streams:
                top = sorted(streams, key=lambda kv: ((kv[1].get("pending") or 0), (kv[1].get("xlen") or 0)),
                             reverse=True)[:3]
                bits = [f"{name} xlen={s.get('xlen') or 0}"
                        + (f" pending={s.get('pending')}" if s.get("pending") is not None else "")
                        for name, s in top]
                lines.append("Redis streams: " + "; ".join(bits) + ".")
            else:
                lines.append("Redis: connected.")

    ext = query.load_ext_latest(conn, since, until, node)
    if ext:
        bad = [source for source, metrics in sorted(ext.items())
               if (metrics.get("up", {}).get("value") or 0.0) < 1.0]
        if bad:
            lines.append(f"External checks: {', '.join(bad)} down.")
        else:
            lines.append(f"External checks: {len(ext)} source(s) ok.")
    ext_events = query.load_ext_events(conn, since, until, node, limit=5)
    if ext_events:
        lines.append("External events: " + "; ".join(
            f"{_hhmm(e['ts'])} {e['source']} {e['event']}" for e in ext_events))

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


# ---------- fleet: aggregated terminal view across all hub nodes ----------
#
# Renders the dicts hubapi.fleet_status()/fleet() return (whether read from the hub DB
# directly or fetched as JSON from /api/{fleet-status,fleet}) as a dense, colour-coded
# table — the terminal twin of the web dashboard. Stdlib + renderer-free like the other
# report surfaces, so `smoke fleet` needs no plotext/matplotlib.

_STATE_SGR = {"healthy": "32", "warn": "33", "down": "31", "stale": "90"}
_STATE_DOT = {"healthy": "●", "warn": "●", "down": "●", "stale": "○"}
_STATE_DOT_ASCII = {"healthy": "+", "warn": "!", "down": "x", "stale": "."}


def _dot(state: str, color: bool) -> str:
    glyph = (_STATE_DOT_ASCII if ASCII else _STATE_DOT).get(state, "?")
    return _sgr(glyph, _STATE_SGR.get(state, "0"), color)


def _fmt_age(a) -> str:
    """Compact relative age, matching the dashboard's fmtAge (s < 90, m < 90, else h)."""
    if a is None:
        return "?"
    if a < 90:
        return f"{a:.0f}s"
    if a < 5400:
        return f"{a / 60:.0f}m"
    return f"{a / 3600:.0f}h"


def fleet_status_report(status: dict, *, color: bool = True) -> str:
    """Latest-sample fleet view (the /api/fleet-status shape): a counts header + one
    worst-first line per node with state dot, WAN RTT/loss, cpu and temp."""
    nodes = status.get("nodes", [])
    c = status.get("counts", {})
    head = (f"FLEET — {len(nodes)} node(s) · "
            + " · ".join(_sgr(f"{c.get(s, 0)} {s}", _STATE_SGR[s], color)
                         for s in ("healthy", "warn", "down", "stale")))
    if not nodes:
        return head + "\n\n(no nodes reporting yet)"
    width = min(22, max(len(n["node"]) for n in nodes))
    lines = [head, ""]
    for n in nodes:
        st = n["state"]
        dot = _dot(st, color)
        name = n["node"][:width].ljust(width)
        if st == "stale":
            right = f"{_fmt_age(n.get('age_s')) + ' ago':>14}"
        else:
            rtt = f"{n['rtt_ms']:.0f}ms" if n.get("rtt_ms") is not None else "--"
            loss = n.get("loss_pct")
            loss_s = _sgr(f"loss{loss:.0f}%", "31", color) if loss else ""
            right = f"{rtt:>7}  {loss_s:<7}"
        extras = []
        if n.get("cpu") is not None:
            extras.append(f"cpu{n['cpu']:.0f}%")
        if n.get("temp") is not None:
            extras.append(f"{n['temp']:.0f}C")
        tail = ("  " + " ".join(extras)) if extras else ""
        lines.append(f"{dot} {name} {right}{tail}")
    return "\n".join(lines)


def fleet_ranked_report(fleet: list, hours: float, *, color: bool = True) -> str:
    """Incident-based ranking (the /api/fleet shape): uptime %, median RTT, incident
    count and hard downtime per node over the window, worst-first."""
    head = f"FLEET RANKED — last {hours:.0f}h · {len(fleet)} node(s), worst first"
    if not fleet:
        return head + "\n\n(no nodes reporting yet)"
    lines = [head, "", f"  {'node':<20} {'uptime':>7} {'rtt':>8} {'inc':>4} {'downtime':>9}"]
    for r in fleet:
        up = f"{r['uptime_pct']:.1f}%" if r.get("uptime_pct") is not None else "--"
        rtt = f"{r['rtt_ms']:.0f}ms" if r.get("rtt_ms") is not None else "--"
        down = analyze._dur(r["downtime_s"]) if r.get("downtime_s") else "-"
        line = (f"  {r['node'][:20]:<20} {up:>7} {rtt:>8} "
                f"{r.get('incidents', 0):>4} {down:>9}")
        degraded = (r.get("uptime_pct") is not None and r["uptime_pct"] < 100.0) or r.get("incidents")
        lines.append(_sgr(line, "31", color) if degraded else line)
    return "\n".join(lines)


_HEAT_WARN_RTT_MS = 250.0  # mirrors hubapi._WARN_RTT_MS: rtt above this colours the cell


def _heat_cell(val, lo, hi, sev, color: bool) -> str:
    """One sparkline cell: block height encodes magnitude, colour encodes severity.
    None/NaN -> a space so gaps in a node's history stay visible."""
    if val is None or val != val:
        return " "
    frac = (val - lo) / ((hi - lo) or 1.0)
    chars = _ramp()
    ch = chars[max(0, min(len(chars) - 1, int(frac * (len(chars) - 1) + 0.5)))]
    code = sev(val)
    return _sgr(ch, code, color) if code else ch


def fleet_heatmap_report(hm: dict, *, color: bool = True) -> str:
    """node × hour heatmap (the /api/heatmap shape) as one sparkline row per node:
    'loss' (max loss%, 0..100) or 'rtt' (median RTT, autoscaled). Cells are coloured by
    severity (green ok / yellow warn / red bad)."""
    metric = hm.get("metric", "loss")
    hours = hm.get("hours", [])
    grid = hm.get("nodes", {})
    unit = "loss%" if metric == "loss" else "median RTT ms"
    head = f"FLEET HEATMAP — {unit}, {len(hours)} hourly buckets (low ▁ … █ high)"
    if not grid:
        return head + "\n\n(no nodes reporting yet)"

    allv = [v for row in grid.values() for v in row if v is not None and v == v]
    if metric == "loss":
        lo, hi = 0.0, 100.0
        def sev(v):
            return "31" if v >= 100.0 else ("33" if v > 0 else "32")
    else:
        lo = min(allv) if allv else 0.0
        hi = max(allv) if allv else 1.0
        def sev(v):
            return "31" if v >= _HEAT_WARN_RTT_MS else ("33" if v >= _HEAT_WARN_RTT_MS / 2 else "32")

    width = min(22, max(len(n) for n in grid))
    lines = [head, ""]
    # Worst-first: nodes with the highest cumulative loss/RTT on top.
    for node in sorted(grid, key=lambda n: sum(v for v in grid[n] if v), reverse=True):
        row = grid[node]
        spark = "".join(_heat_cell(v, lo, hi, sev, color) for v in row)
        lines.append(f"{node[:width].ljust(width)} {spark}")
    if len(hours) >= 2:
        start, end = _hhmm(hours[0]), _hhmm(hours[-1])
        gap = max(1, len(hours) - len(start) - len(end))
        lines.append(" " * (width + 1) + start + " " * gap + end)
    return "\n".join(lines)
