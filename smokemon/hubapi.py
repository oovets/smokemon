"""Read-only query layer behind the hub's GET endpoints: a Prometheus/OpenMetrics
exposition (S2) and a small JSON API with a fleet ranking and a node×hour heatmap
(S3). Pure stdlib, derives everything from the hub DB via direct SQL + the shared
analysis engine. Split out from hub.py so it can be unit-tested without a socket.

Latest-value queries lean on SQLite's documented bare-column behaviour: with a
MAX(ts) aggregate and GROUP BY node, the other selected columns come from the row
that holds that max ts - i.e. the most recent sample per node."""

import sqlite3
import time

from . import analyze, config, query, schema


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def nodes(conn) -> list[str]:
    """Distinct node names known to the hub DB."""
    seen: set[str] = set()
    for t in ("host_samples", "ping_runs", "net_samples"):
        for (n,) in _rows(conn, f"SELECT DISTINCT node FROM {t}"):
            if n:
                seen.add(n)
    return sorted(seen)


def latest_metrics(conn) -> dict[str, dict]:
    """{node: {cpu, mem, temp, ts, targets:{name:{rtt,loss}}}} most-recent values."""
    out: dict[str, dict] = {}
    for node, cpu, mem, temp, ts in _rows(
            conn, "SELECT node, cpu_pct, mem_used_pct, temp_c, MAX(ts) FROM host_samples GROUP BY node"):
        out.setdefault(node, {})["cpu"] = cpu
        out[node]["mem"] = mem
        out[node]["temp"] = temp
        out[node]["ts"] = ts
    for node, target, rtt, loss, ts in _rows(
            conn, "SELECT node, target, rtt_median, loss_pct, MAX(ts) FROM ping_runs GROUP BY node, target"):
        d = out.setdefault(node, {}).setdefault("targets", {})
        d[target] = {"rtt_ms": rtt, "loss_pct": loss, "ts": ts}
    return out


# ---------- S2: Prometheus / OpenMetrics ----------


def _metric(name, help_text, typ, samples) -> list[str]:
    """One metric block (HELP/TYPE + samples). samples: [(labels_str, value)]."""
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}"]
    for labels, value in samples:
        if value is None:
            continue
        out.append(f"{name}{{{labels}}} {value}")
    return out


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def prometheus(conn) -> str:
    """OpenMetrics text exposition of the latest gauges per node. Plugs straight into a
    Prometheus scrape -> Grafana / Alertmanager."""
    latest = latest_metrics(conn)
    rtt, loss, cpu, mem, temp = [], [], [], [], []
    for node, d in sorted(latest.items()):
        nl = f'node="{_esc(node)}"'
        if "cpu" in d:
            cpu.append((nl, d.get("cpu")))
            mem.append((nl, d.get("mem")))
            temp.append((nl, d.get("temp")))
        for target, td in sorted(d.get("targets", {}).items()):
            tl = f'{nl},target="{_esc(target)}"'
            rtt.append((tl, td.get("rtt_ms")))
            loss.append((tl, td.get("loss_pct")))
    lines: list[str] = []
    lines += _metric("smokemon_ping_rtt_ms", "Median ping RTT (ms), latest sample", "gauge", rtt)
    lines += _metric("smokemon_ping_loss_pct", "Ping loss percent, latest sample", "gauge", loss)
    lines += _metric("smokemon_cpu_pct", "CPU utilisation percent, latest sample", "gauge", cpu)
    lines += _metric("smokemon_mem_pct", "Memory used percent, latest sample", "gauge", mem)
    lines += _metric("smokemon_temp_c", "Temperature (C), latest sample", "gauge", temp)
    return "\n".join(lines) + "\n"


# ---------- S3: fleet ranking + heatmap ----------


def fleet(conn, hours: float = 24.0, until: float | None = None) -> list[dict]:
    """Per-node health over the last `hours`, ranked worst-first: internet uptime %,
    median RTT, incident count. Uses the same detector as `smoke incidents`."""
    until = time.time() if until is None else until
    since = until - hours * 3600
    out = []
    for node in nodes(conn):
        ping = query.load_ping_agg(conn, since, until, None, node)
        http = query.load_http(conn, since, until, node)
        incidents = analyze.detect_incidents(ping, http)
        cls = analyze.classify_targets(ping)
        inet = cls["internet"] or list(ping)
        up_pct, rtt_med = None, None
        if inet:
            losses, meds = [], []
            for name in inet:
                losses += ping[name]["loss"]
                meds += [m for m in ping[name]["med"] if m is not None]
            if losses:
                up_pct = round(100.0 * sum(1 for x in losses if (x or 0) < 100.0) / len(losses), 2)
            rtt_med = round(analyze._median(meds), 1) if meds else None
        hard = analyze.merge_spans([(i["start"], i["end"]) for i in incidents
                                    if i["klass"] in ("isp-outage", "link-down")])
        out.append({
            "node": node, "uptime_pct": up_pct, "rtt_ms": rtt_med,
            "incidents": len(incidents),
            "downtime_s": round(sum(e - s for s, e in hard), 1),
        })
    # Worst first: least uptime, then most downtime, then most incidents.
    out.sort(key=lambda r: (r["uptime_pct"] if r["uptime_pct"] is not None else 100.0,
                            -r["downtime_s"], -r["incidents"]))
    return out


def heatmap(conn, metric: str = "loss", hours: float = 24.0, until: float | None = None) -> dict:
    """node × hour grid for 'loss' (max loss%) or 'rtt' (median RTT). Returns
    {metric, hours:[epoch...], nodes:{node:[val per hour]}}."""
    until = time.time() if until is None else until
    since = until - hours * 3600
    n_buckets = int(hours)
    hour0 = since - (since % 3600)
    col = "loss_pct" if metric == "loss" else "rtt_median"
    agg = "MAX" if metric == "loss" else "AVG"
    grid: dict[str, list] = {}
    rows = _rows(conn,
                 f"SELECT node, CAST((ts - ?) / 3600 AS INT) hr, {agg}({col}) "
                 "FROM ping_runs WHERE ts BETWEEN ? AND ? GROUP BY node, hr",
                 (hour0, since, until))
    for node, hr, val in rows:
        if node is None or hr is None:
            continue
        series = grid.setdefault(node, [None] * (n_buckets + 1))
        if 0 <= hr < len(series):
            series[hr] = round(val, 1) if val is not None else None
    return {"metric": metric, "hours": [hour0 + i * 3600 for i in range(n_buckets + 1)], "nodes": grid}


# ---------- S6: live fleet dashboard ----------

# At-a-glance state thresholds. Deliberately latest-sample based (no incident
# detection) so the endpoint stays a handful of GROUP BY queries even at 150 nodes;
# the deep view is `smoke incidents` / /api/fleet.
_WARN_RTT_MS = 250.0          # reachable but high internet RTT -> warn
_WARN_LOSS_PCT = 1.0          # sustained loss at/above this -> warn
FLEET_STALE_AFTER_S = 300.0   # no fresh sample within this -> stale/offline
_STATE_ORDER = {"down": 0, "stale": 1, "warn": 2, "healthy": 3}


def _wan_target(targets: dict) -> str | None:
    """WAN-representative target from a latest_metrics targets dict: prefer those
    classified 'internet' (largest RTT among them), else the largest-RTT target."""
    if not targets:
        return None
    inet = [n for n in targets if analyze.classify_target(n) == "internet"] or list(targets)
    return max(inet, key=lambda n: targets[n].get("rtt_ms") or 0.0)


def _node_state(age_s, loss, rtt, temp, stale_after_s) -> str:
    if age_s is None or age_s > stale_after_s:
        return "stale"
    if loss is not None and loss >= 100.0:
        return "down"
    if ((loss is not None and loss >= _WARN_LOSS_PCT)
            or (rtt is not None and rtt > _WARN_RTT_MS)
            or (temp is not None and temp >= config.THROTTLE_TEMP_C - 5.0)):
        return "warn"
    return "healthy"


def fleet_status(conn, stale_after_s: float = FLEET_STALE_AFTER_S, now: float | None = None) -> dict:
    """Fast per-node status for the live dashboard: one latest_metrics() pass (a few
    GROUP BY queries, no incident detection) -> derived state + key gauges, sorted
    worst-first. Returns {now, stale_after_s, counts:{state:n}, nodes:[{node, state,
    rtt_ms, loss_pct, cpu, temp, age_s}]}."""
    now = time.time() if now is None else now
    latest = latest_metrics(conn)
    counts = {"healthy": 0, "warn": 0, "down": 0, "stale": 0}
    out = []
    for node, d in latest.items():
        targets = d.get("targets", {})
        tgt = _wan_target(targets)
        rtt = loss = None
        tstamps = [d.get("ts")]
        if tgt:
            rtt = targets[tgt].get("rtt_ms")
            loss = targets[tgt].get("loss_pct")
            tstamps.append(targets[tgt].get("ts"))
        last_ts = max([t for t in tstamps if t is not None], default=None)
        age = (now - last_ts) if last_ts is not None else None
        state = _node_state(age, loss, rtt, d.get("temp"), stale_after_s)
        counts[state] += 1
        out.append({"node": node, "state": state, "rtt_ms": rtt, "loss_pct": loss,
                    "cpu": d.get("cpu"), "mem": d.get("mem"), "temp": d.get("temp"),
                    "age_s": round(age) if age is not None else None})
    out.sort(key=lambda r: (_STATE_ORDER[r["state"]],
                            -(r["loss_pct"] or 0.0), -(r["rtt_ms"] or 0.0), r["node"]))
    return {"now": now, "stale_after_s": stale_after_s, "counts": counts, "nodes": out}


def sparklines(conn, hours: float = 2.0, buckets: int = 30, now: float | None = None) -> dict:
    """Compact recent internet-RTT trend per node for inline grid sparklines: one bucketed
    GROUP BY over the last `hours`, averaged across internet-classified targets (falls back
    to all targets). Returns {node: [rtt|null per bucket]} - small enough to poll cheaply."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    width = max(1.0, (hours * 3600) / buckets)
    per: dict[str, dict[str, dict[int, float]]] = {}
    for node, target, b, val in _rows(
            conn, "SELECT node, target, CAST((ts - ?) / ? AS INT) b, AVG(rtt_median) "
            "FROM ping_runs WHERE ts >= ? GROUP BY node, target, b", (since, width, since)):
        if node is None or b is None or not (0 <= b < buckets):
            continue
        per.setdefault(node, {}).setdefault(target, {})[int(b)] = val
    out: dict[str, list] = {}
    for node, tmap in per.items():
        inet = [t for t in tmap if analyze.classify_target(t) == "internet"] or list(tmap)
        series: list = [None] * buckets
        for b in range(buckets):
            vals = [tmap[t][b] for t in inet if b in tmap[t] and tmap[t][b] is not None]
            if vals:
                series[b] = round(sum(vals) / len(vals), 1)
        out[node] = series
    return out


def risks(conn, hours: float = 24.0, now: float | None = None) -> dict:
    """Fleet-wide 'what's failing / about to fail': death-clock ETAs (disk-full, SD wear,
    thermal headroom) sorted soonest-first, plus the recent incident feed. Reuses the same
    detectors + ETA projections as `smoke incidents` / the PNG titles. On-demand (the risks
    tab fetches it), so the per-node loads stay off the 5s status path."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    # Only surface clocks that are actually actionable: a far-future projection (flat usage ->
    # huge ETA) is noise, not a death clock. Disk-full within a year, SD wear within five.
    disk_horizon, wear_horizon = 365 * 86400, 5 * 365 * 86400
    clocks: list[dict] = []
    incidents: list[dict] = []
    for node in nodes(conn):
        ping = query.load_ping_agg(conn, since, now, None, node)
        http = query.load_http(conn, since, now, node)
        for inc in analyze.detect_incidents(ping, http):
            incidents.append({**inc, "node": node})
        # drop read-only pseudo-mounts (snap/loop squashfs sit at 100% by design -> false "full")
        disk = {m: v for m, v in query.load_disk(conn, since, now, node).items()
                if not (m.startswith("/snap") or m.startswith("/var/snap") or "/snapd/" in m)}
        full = query.disk_full_eta(disk)
        if full and full[1] <= disk_horizon:
            clocks.append({"node": node, "kind": "disk", "eta_s": round(full[1]),
                           "detail": f"{full[0]} full {query.human_eta(full[1])}"})
        wear = query.wear_eta(query.load_disk_health(conn, since, now, node))
        if wear and wear[1] <= wear_horizon:
            clocks.append({"node": node, "kind": "sd-wear", "eta_s": round(wear[1]),
                           "detail": f"{wear[0]} wear {query.human_eta(wear[1])}"})
        host = query.load_host(conn, since, now, node)
        temp = query.last_value(host.get("temp", [])) if host else None
        if temp is not None:
            head = config.THROTTLE_TEMP_C - temp
            if head <= 10.0:  # only surface when near the throttle ceiling
                clocks.append({"node": node, "kind": "throttle", "eta_s": None,
                               "detail": f"{temp:.0f}C ({head:.0f}C to throttle)" if head > 0
                               else f"{temp:.0f}C THROTTLING"})
    clocks.sort(key=lambda c: (c["eta_s"] is None, c["eta_s"] or 0))
    incidents.sort(key=lambda i: -i["start"])
    return {"now": now, "hours": hours, "clocks": clocks, "incidents": incidents[:50]}


def ship_volume(conn, hours: float = 24.0, now: float | None = None) -> dict:
    """Measured ship cost per node: the ACTUAL compressed bytes each node pushed over the wire
    (summed from ingest_log, which records every POST's Content-Length), not a from-the-DB
    estimate. Answers 'is this node shipping a lot / wasteful data?'. Also returns the per-table
    row counts received in the window so you can see WHICH data dominates (e.g. a node shipping
    raw ping_rtts). Sorted heaviest-first. ingest_log only accrues from hub start, so a fresh
    hub shows little until traffic flows."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    agg: dict[str, dict] = {}
    for node, wire, raw, rows, posts, mn, mx in _rows(
            conn, "SELECT node, SUM(wire_bytes), SUM(raw_bytes), SUM(rows), COUNT(*), MIN(ts), MAX(ts) "
            "FROM ingest_log WHERE ts >= ? GROUP BY node", (since,)):
        if node is None:
            continue
        span = (mx - mn) if (mn is not None and mx is not None and mx > mn) else 0.0
        # only extrapolate to /day once the measured window is long enough to be representative
        # (the first post after a (re)start ships accumulated backlog and skews a short span);
        # below that the frontend shows the raw window total instead.
        rate_ok = span >= 600.0
        per_day = (wire * 86400.0 / span) if rate_ok else None
        rpd = (rows * 86400.0 / span) if rate_ok else None
        agg[node] = {"node": node, "wire_bytes": int(wire or 0), "raw_bytes": int(raw or 0),
                     "rows": int(rows or 0), "posts": int(posts or 0), "observed_s": round(span),
                     "wire_bytes_per_day": round(per_day) if per_day else None,
                     "rows_per_day": round(rpd) if rpd else None,
                     "ratio": round(raw / wire, 1) if wire else None, "top": []}
    # per-table rows received in the window -> the "what kind of data" breakdown (one query/table)
    tabrows: dict[str, dict[str, int]] = {}
    for table in schema.STD_TABLES:
        for node, c in _rows(conn, f"SELECT node, COUNT(*) FROM {table} WHERE ts >= ? GROUP BY node", (since,)):
            if node is not None:
                tabrows.setdefault(node, {})[table] = c
    for node, d in agg.items():
        top = sorted(tabrows.get(node, {}).items(), key=lambda kv: -kv[1])[:3]
        d["top"] = [{"t": t, "n": c} for t, c in top]
    out = sorted(agg.values(), key=lambda r: -(r["wire_bytes"] or 0))
    return {"now": now, "hours": hours, "nodes": out}


def ingest_rate(events, now: float | None = None, window_s: float = 900.0,
                rate_window_s: float = 60.0, buckets: int = 60) -> dict:
    """Live hub ingest throughput, derived from the POST /ingest handler's in-memory ring buffer
    (a list of (ts, wire_bytes, raw_bytes, rows) tuples - never persisted, so this stays cheap and
    socket-free for unit tests). Returns the recent wire bytes/sec and rows/sec over the last
    `rate_window_s` (the gauge value), a per-bucket wire-bytes series over the last `window_s` for a
    sparkline, the window totals and the most-recent ingest timestamp.

    `window_s` should match the buffer's retention so the series can't reference dropped events."""
    now = time.time() if now is None else now
    since = now - window_s
    width = window_s / buckets
    rate_since = now - rate_window_s
    series = [0] * buckets
    total_wire = total_raw = total_rows = posts = 0
    recent_wire = recent_rows = 0
    last_ts: float | None = None
    for ts, wire, raw, rows in events:
        if ts < since:
            continue
        posts += 1
        total_wire += wire or 0
        total_raw += raw or 0
        total_rows += rows or 0
        b = int((ts - since) / width)
        if 0 <= b < buckets:
            series[b] += wire or 0
        if last_ts is None or ts > last_ts:
            last_ts = ts
        if ts >= rate_since:
            recent_wire += wire or 0
            recent_rows += rows or 0
    return {
        "now": now, "window_s": window_s, "rate_window_s": rate_window_s,
        "bucket_s": width, "buckets": buckets,
        "bytes_per_s": round(recent_wire / rate_window_s, 1) if rate_window_s else 0.0,
        "rows_per_s": round(recent_rows / rate_window_s, 3) if rate_window_s else 0.0,
        "series_bytes": series, "total_wire_bytes": total_wire,
        "total_raw_bytes": total_raw, "total_rows": total_rows,
        "posts": posts, "last_ts": last_ts,
    }


def services(conn, hours: float = 168.0, now: float | None = None) -> dict:
    """Fleet-wide latest service telemetry for the dashboard 'services' table: Docker
    containers, Redis instances (+ their hottest streams), watched processes and stream
    probes, each as the most-recent row per (node, entity) via the MAX(ts) bare-column
    trick (a few GROUP BY queries, no per-node loops). Bounded to rows newer than `hours`
    so the scan stays cheap and long-removed containers/streams age out instead of lingering
    forever. Returns {now, docker, docker_down, redis, procs, streams}; empty lists when a
    probe is unused fleet-wide."""
    now = time.time() if now is None else now
    since = now - hours * 3600

    def age(ts):
        return round(now - ts) if ts is not None else None

    docker, docker_down = [], []
    for (node, name, state, running, health, exit_code, restart_count,
         oom, cpu, mem, pids, ts) in _rows(
            conn, "SELECT node, name, state, running, health, exit_code, restart_count, "
            "oom_killed, cpu_pct, mem_mb, pids, MAX(ts) FROM docker_samples "
            "WHERE ts >= ? GROUP BY node, name", (since,)):
        if node is None:
            continue
        if name == "__daemon__":
            if not running:
                docker_down.append(node)
            continue
        v = {"node": node, "name": name, "state": state, "running": running,
             "health": health, "exit_code": exit_code, "restart_count": restart_count,
             "oom_killed": oom, "cpu_pct": cpu, "mem_mb": mem, "pids": pids, "age_s": age(ts)}
        v["bad"] = query.docker_bad(v)
        docker.append(v)
    docker.sort(key=lambda r: (not r["bad"], bool(r["running"]), r["node"], r["name"]))

    redis_map: dict[tuple, dict] = {}
    for (node, instance, connected, mem, clients, blocked, ops, evicted, rejected, ts) in _rows(
            conn, "SELECT node, instance, connected, used_memory_mb, connected_clients, "
            "blocked_clients, ops_per_sec, evicted_keys, rejected_connections, MAX(ts) "
            "FROM redis_samples WHERE stream = '__server__' AND ts >= ? GROUP BY node, instance", (since,)):
        if node is None:
            continue
        redis_map[(node, instance)] = {
            "node": node, "instance": instance, "connected": connected, "used_memory_mb": mem,
            "connected_clients": clients, "blocked_clients": blocked, "ops_per_sec": ops,
            "evicted_keys": evicted, "rejected_connections": rejected, "age_s": age(ts), "streams": []}
    for node, instance, stream, xlen, pending, _ts in _rows(
            conn, "SELECT node, instance, stream, xlen, pending, MAX(ts) FROM redis_samples "
            "WHERE stream IS NOT NULL AND stream != '__server__' AND ts >= ? "
            "GROUP BY node, instance, stream", (since,)):
        if node is None:
            continue
        entry = redis_map.setdefault((node, instance), {
            "node": node, "instance": instance, "connected": None, "used_memory_mb": None,
            "connected_clients": None, "blocked_clients": None, "ops_per_sec": None,
            "evicted_keys": None, "rejected_connections": None, "age_s": None, "streams": []})
        entry["streams"].append({"stream": stream, "xlen": xlen, "pending": pending})
    for entry in redis_map.values():
        entry["streams"].sort(key=lambda s: ((s["pending"] or 0), (s["xlen"] or 0)), reverse=True)
        del entry["streams"][3:]  # keep the three hottest streams per instance
    redis = sorted(redis_map.values(), key=lambda r: ((r["connected"] or 0) >= 1, r["node"]))

    procs = []
    for node, label, count, cpu, rss, uptime, restarts, ts in _rows(
            conn, "SELECT node, label, count, cpu_pct, rss_mb, uptime_s, restarts, MAX(ts) "
            "FROM proc_watch WHERE ts >= ? GROUP BY node, label", (since,)):
        if node is None:
            continue
        procs.append({"node": node, "label": label, "count": count, "cpu_pct": cpu,
                      "rss_mb": rss, "uptime_s": uptime, "restarts": restarts, "age_s": age(ts)})
    procs.sort(key=lambda r: (bool(r["count"]), r["node"], r["label"]))

    streams = []
    for node, url, ok, latency, status, ts in _rows(
            conn, "SELECT node, url, ok, latency_ms, status, MAX(ts) FROM stream_probes "
            "WHERE ts >= ? GROUP BY node, url", (since,)):
        if node is None:
            continue
        streams.append({"node": node, "url": url, "ok": ok, "latency_ms": latency,
                        "status": status, "age_s": age(ts)})
    streams.sort(key=lambda r: (bool(r["ok"]), r["node"], r["url"]))

    return {"now": now, "docker": docker, "docker_down": sorted(set(docker_down)),
            "redis": redis, "procs": procs, "streams": streams}


def dashboard_html() -> str:
    """Self-contained fleet dashboard (no external assets). Polls /api/fleet-status and
    renders an ultra-dense, worst-first, colour-coded one-line-per-node grid. Refresh
    interval via ?refresh=SEC (default 5)."""
    return _DASHBOARD_HTML


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>smokemon fleet</title>
<style>
 :root{
  --bg:#0a0d13;--bg2:#0d1119;--card:#10151e;--card2:#161c27;--line:#1e2533;--line2:#2b3442;
  --fg:#e6edf3;--mut:#97a1b0;--dim:#646f7e;
  --ok:#3fb950;--okf:#56d364;--warn:#d99a1c;--warnf:#e3b341;--down:#f85149;--downf:#ff7b72;
  --stale:#566071;--stalef:#9aa4b2;--accent:#58a6ff;
  --ok-bg:rgba(63,185,80,.14);--warn-bg:rgba(227,179,65,.14);--down-bg:rgba(248,81,73,.14);
  --stale-bg:rgba(120,131,148,.13);--accent-bg:rgba(88,166,255,.13);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --r:12px;--sh:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.22)}
 *{box-sizing:border-box}
 ::selection{background:rgba(88,166,255,.3)}
 ::-webkit-scrollbar{width:11px;height:11px}
 ::-webkit-scrollbar-track{background:transparent}
 ::-webkit-scrollbar-thumb{background:var(--line2);border-radius:7px;border:3px solid var(--bg)}
 ::-webkit-scrollbar-thumb:hover{background:#3a4453}
 body{margin:0;color:var(--fg);font:13px/1.45 var(--sans);background:var(--bg);
   background-image:radial-gradient(1100px 560px at 82% -12%,rgba(88,166,255,.06),transparent 60%),
     radial-gradient(900px 500px at -5% -5%,rgba(124,131,255,.05),transparent 55%);
   background-attachment:fixed;-webkit-font-smoothing:antialiased}
 .num{font-family:var(--mono);font-variant-numeric:tabular-nums}
 /* ---- header ---- */
 header{position:sticky;top:0;z-index:30;background:rgba(10,13,19,.82);
   backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
 .hrow{display:flex;gap:16px;align-items:center;padding:9px 16px 0;flex-wrap:wrap}
 .hrow2{display:flex;gap:14px;align-items:center;padding:9px 16px;flex-wrap:wrap}
 .brand{display:flex;align-items:center;gap:9px;flex:0 0 auto}
 .brand svg{display:block;filter:drop-shadow(0 0 6px rgba(88,166,255,.5))}
 h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.3px;color:var(--fg)}
 h1 b{color:var(--accent);font-weight:700;letter-spacing:1.5px;font-size:11px;padding:2px 7px;
   border:1px solid var(--accent);border-radius:6px;margin-left:4px;background:var(--accent-bg)}
 .tabs{display:flex;gap:3px;flex-wrap:wrap}
 .tab{padding:5px 12px;border-radius:8px;cursor:pointer;color:var(--mut);font-size:12.5px;
   font-weight:500;border:1px solid transparent;transition:.14s}
 .tab:hover{color:var(--fg);background:var(--card)}
 .tab.on{color:var(--fg);background:var(--card2);border-color:var(--line2);box-shadow:inset 0 -2px 0 var(--accent)}
 .meta{color:var(--dim);font-size:11.5px;margin-left:auto;font-family:var(--mono);white-space:nowrap}
 input#q{background:var(--card);border:1px solid var(--line2);color:var(--fg);padding:6px 10px;
   border-radius:8px;font:13px var(--sans);min-width:170px;outline:none;transition:.14s}
 input#q:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-bg)}
 input#q::placeholder{color:var(--dim)}
 .healthband{flex:1 1 260px;display:flex;align-items:center;gap:11px;min-width:180px}
 .hb-label{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);flex:0 0 auto}
 .hb-track{flex:1 1 auto;height:10px;border-radius:6px;overflow:hidden;display:flex;gap:2px;
   background:var(--card);border:1px solid var(--line);min-width:120px;padding:1px}
 .hb-seg{height:100%;border-radius:3px;transition:width .5s ease;min-width:0}
 .hb-seg.healthy{background:linear-gradient(90deg,var(--ok),var(--okf))}
 .hb-seg.warn{background:linear-gradient(90deg,var(--warn),var(--warnf))}
 .hb-seg.down{background:linear-gradient(90deg,var(--down),var(--downf))}
 .hb-seg.stale{background:var(--stale)}
 .pills{display:flex;gap:7px;flex:0 0 auto;flex-wrap:wrap}
 .pill{padding:3px 11px 3px 9px;border-radius:20px;font-size:11.5px;display:flex;gap:7px;
   align-items:center;border:1px solid var(--line);background:var(--card)}
 .pill .pc{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:700}
 .pill .pl{color:var(--mut)}
 .pill.down .pc{color:var(--downf)}.pill.warn .pc{color:var(--warnf)}.pill.healthy .pc{color:var(--okf)}
 .dot,.st{width:8px;height:8px;border-radius:50%;flex:0 0 auto;display:inline-block;vertical-align:middle}
 .st{width:9px;height:9px}
 .s-healthy{background:var(--okf);box-shadow:0 0 7px rgba(63,185,80,.7)}
 .s-warn{background:var(--warnf);box-shadow:0 0 7px rgba(227,179,65,.6)}
 .s-down{background:var(--downf);box-shadow:0 0 7px rgba(248,81,73,.7)}
 .s-stale{background:var(--stale)}
 #err{color:var(--downf);padding:7px 16px;font-family:var(--mono);font-size:12px;
   background:var(--down-bg);border-bottom:1px solid rgba(248,81,73,.25)}
 #err:empty{display:none}
 /* ---- generic primitives ---- */
 .card{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line);
   border-radius:var(--r);box-shadow:var(--sh)}
 .card-h{display:flex;align-items:center;gap:8px;font-size:11px;text-transform:uppercase;
   letter-spacing:.9px;color:var(--mut);font-weight:600;padding:13px 16px 0}
 .card-h .card-sub{margin-left:auto;text-transform:none;letter-spacing:0;color:var(--dim);
   font-weight:400;font-family:var(--mono);font-size:11px}
 .view{padding:16px;animation:fade .25s ease}
 .view[hidden]{display:none}
 @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
 .empty{color:var(--dim);padding:30px 16px;text-align:center;font-style:italic}
 .view h2{font-size:11px;color:var(--mut);font-weight:600;letter-spacing:.9px;text-transform:uppercase;
   margin:24px 2px 11px;display:flex;align-items:center;gap:9px}
 .view h2:first-child{margin-top:2px}
 .view h2 .cnt{font-family:var(--mono);color:var(--dim);font-weight:400;font-size:11px}
 .btn-grp{display:flex;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px}
 .btn-grp button{background:none;border:none;color:var(--mut);font:500 12px var(--sans);
   padding:4px 11px;border-radius:6px;cursor:pointer;transition:.12s}
 .btn-grp button:hover{color:var(--fg)}
 .btn-grp button.on{background:var(--card2);color:var(--fg);box-shadow:inset 0 0 0 1px var(--line2)}
 td.bad,.bad{color:var(--downf)}td.warnv,.warnv{color:var(--warnf)}td.okv,.okv{color:var(--okf)}
 /* ---- overview (grid tab) ---- */
 .ov{display:flex;flex-direction:column;gap:16px;max-width:1320px;margin:0 auto}
 .ov-hero{display:grid;grid-template-columns:minmax(280px,1fr) minmax(360px,1.5fr);gap:16px}
 @media(max-width:780px){.ov-hero{grid-template-columns:1fr}}
 .donut-wrap{display:flex;align-items:center;gap:18px;padding:10px 16px 16px}
 .donut{width:144px;height:144px;flex:0 0 auto}
 .donut .track{fill:none;stroke:var(--card2);stroke-width:13}
 .donut .seg{fill:none;stroke-width:13;transform:rotate(-90deg);transform-origin:60px 60px;
   transition:stroke-dasharray .6s ease,stroke-dashoffset .6s ease}
 .donut .seg.healthy{stroke:var(--okf)}.donut .seg.warn{stroke:var(--warnf)}
 .donut .seg.down{stroke:var(--downf)}.donut .seg.stale{stroke:var(--stale)}
 .donut-total{font:700 32px/1 var(--mono);fill:var(--fg)}
 .donut-cap{font:600 9px var(--sans);fill:var(--dim);letter-spacing:1.5px;text-transform:uppercase}
 .donut-legend{display:flex;flex-direction:column;gap:9px;flex:1 1 auto}
 .lg{display:flex;align-items:center;gap:9px;font-size:12.5px}
 .lg .lg-label{color:var(--mut);flex:1 1 auto;text-transform:capitalize}
 .lg .lg-count{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:700;font-size:15px}
 .lg.healthy .lg-count{color:var(--okf)}.lg.warn .lg-count{color:var(--warnf)}
 .lg.down .lg-count{color:var(--downf)}.lg.stale .lg-count{color:var(--stalef)}
 .ingest-card{display:flex;flex-direction:column}
 .gauge-row{display:flex;align-items:baseline;gap:8px;padding:8px 16px 0}
 .gval{font:700 40px/1 var(--mono);font-variant-numeric:tabular-nums;color:#9ea4ff;
   text-shadow:0 0 26px rgba(124,131,255,.4)}
 .gunit{font-size:14px;color:var(--mut);font-weight:500}
 .igspark{width:100%;height:96px;display:block;margin-top:2px}
 .gsub{color:var(--dim);font-size:11.5px;font-family:var(--mono);padding:0 16px 14px}
 .kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
 .kpi{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line);
   border-radius:var(--r);padding:14px 15px;position:relative;overflow:hidden;box-shadow:var(--sh)}
 .kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line2)}
 .kpi.ok::before{background:var(--ok)}.kpi.warn::before{background:var(--warn)}.kpi.bad::before{background:var(--down)}
 .kpi .tv{font:700 27px/1.05 var(--mono);font-variant-numeric:tabular-nums;color:var(--fg)}
 .kpi.ok .tv{color:var(--okf)}.kpi.warn .tv{color:var(--warnf)}.kpi.bad .tv{color:var(--downf)}
 .kpi .tl{color:var(--mut);font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;margin-top:7px}
 .kpi .meter{height:5px;border-radius:4px;background:var(--card2);margin-top:10px;overflow:hidden}
 .kpi .meter-fill{height:100%;border-radius:4px;width:0;transition:width .5s ease;background:var(--ok)}
 .kpi.warn .meter-fill{background:var(--warn)}.kpi.bad .meter-fill{background:var(--down)}
 /* ---- per-node view (table tab) ---- */
 .nodebar{display:flex;align-items:center;gap:10px;margin-bottom:13px}
 .seg-ctl{display:flex;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px}
 .seg-ctl button{background:none;border:none;color:var(--mut);font:500 12px var(--sans);
   padding:4px 12px;border-radius:6px;cursor:pointer;transition:.12s}
 .seg-ctl button.on{background:var(--card2);color:var(--fg);box-shadow:inset 0 0 0 1px var(--line2)}
 .nodebar .cnt{color:var(--dim);font-size:11.5px;font-family:var(--mono);margin-left:auto}
 table.grid-t{border-collapse:separate;border-spacing:0;width:100%;font-size:12.5px}
 .grid-t th{background:var(--bg2);color:var(--mut);font-weight:600;text-transform:uppercase;
   font-size:10.5px;letter-spacing:.5px;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line2);
   white-space:nowrap;user-select:none}
 .grid-t th[data-sort]{cursor:pointer}.grid-t th[data-sort]:hover{color:var(--fg)}
 .grid-t th:first-child,.grid-t th:nth-child(2){text-align:left}
 .grid-t th .ar{color:var(--accent);font-size:9px}
 .grid-t td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;
   font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--fg)}
 .grid-t td.tname{text-align:left;font-family:var(--sans);font-weight:500}
 .grid-t td.stcell{text-align:center;width:34px}
 .grid-t tbody tr{cursor:pointer;transition:background .12s}
 .grid-t tbody tr:hover{background:var(--card)}
 .grid-t tr.stale td:not(.stcell):not(.tname){color:var(--stalef)}
 .spark{width:66px;height:18px;display:inline-block;vertical-align:middle}
 .mini{display:inline-flex;align-items:center;gap:7px;justify-content:flex-end}
 .mini .mbar{width:42px;height:5px;border-radius:3px;background:var(--card2);overflow:hidden;display:inline-block}
 .mini .mbar i{display:block;height:100%;background:var(--ok);border-radius:3px}
 .mini b{font-weight:600;color:var(--fg)}
 .mini.warn .mbar i{background:var(--warn)}.mini.bad .mbar i{background:var(--down)}
 .mini.warn b{color:var(--warnf)}.mini.bad b{color:var(--downf)}
 .tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(214px,1fr));gap:12px}
 .ntile{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line);
   border-left:3px solid var(--stale);border-radius:var(--r);padding:12px 13px;cursor:pointer;
   box-shadow:var(--sh);transition:.14s}
 .ntile:hover{border-color:var(--line2);transform:translateY(-2px)}
 .ntile.healthy{border-left-color:var(--ok)}.ntile.warn{border-left-color:var(--warn)}
 .ntile.down{border-left-color:var(--down)}.ntile.stale{border-left-color:var(--stale);opacity:.74}
 .ntile-h{display:flex;align-items:center;gap:8px}
 .ntile-h .nm{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto}
 .ntile-spark{width:100%;height:30px;display:block;margin:8px 0}
 .ntile-m{display:flex;gap:11px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--mut)}
 .ntile-m b{color:var(--fg);font-weight:600}
 .chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:20px;
   font:600 10px var(--sans);text-transform:uppercase;letter-spacing:.4px}
 .chip.healthy{background:var(--ok-bg);color:var(--okf)}.chip.warn{background:var(--warn-bg);color:var(--warnf)}
 .chip.down{background:var(--down-bg);color:var(--downf)}.chip.stale{background:var(--stale-bg);color:var(--stalef)}
 /* ---- heatmap ---- */
 #heat{overflow-x:auto}
 .heat-tools{display:flex;gap:14px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
 .heat-legend{display:flex;align-items:center;gap:8px;margin-left:auto;font-size:11px;
   color:var(--dim);font-family:var(--mono)}
 .heat-legend .bar{width:130px;height:10px;border-radius:6px;border:1px solid var(--line)}
 .heatgrid{display:inline-block;min-width:100%}
 .hrow{display:flex;align-items:center;gap:10px;margin-bottom:3px}
 .hname{width:140px;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;
   font-size:12px;font-weight:500;text-align:right;color:var(--mut)}
 .hname:hover{color:var(--fg)}
 .hcells{display:flex;gap:2px}
 .hcell{width:15px;height:15px;border-radius:3px;flex:0 0 auto;transition:transform .1s}
 .hcell:hover{transform:scale(1.5);outline:1px solid var(--fg);position:relative;z-index:2}
 .haxis{display:flex;gap:2px;margin:7px 0 0 150px;color:var(--dim);font-size:10px;font-family:var(--mono)}
 .haxis span{flex:0 0 auto;width:15px;text-align:center;overflow:visible}
 /* ---- risks ---- */
 #risk{max-width:1120px}
 .risk{display:flex;gap:12px;align-items:center;padding:10px 14px;border-radius:10px;cursor:pointer;
   background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line2);
   margin-bottom:7px;transition:.12s}
 .risk:hover{border-color:var(--line2);background:var(--card2)}
 .risk .ic{width:18px;height:18px;flex:0 0 auto;color:var(--mut);display:flex}
 .risk .rk{flex:0 0 auto;width:92px;font-size:10px;text-transform:uppercase;letter-spacing:.6px;
   color:var(--mut);font-weight:700}
 .risk .rn{flex:0 0 auto;width:150px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .risk .rd{color:var(--mut);font-size:12.5px;font-family:var(--mono);flex:1 1 auto}
 .risk .reta{flex:0 0 auto;font-family:var(--mono);font-weight:700;font-size:13px;margin-left:auto;color:var(--fg)}
 .risk.disk,.risk.throttle,.risk.sev3{border-left-color:var(--down)}
 .risk.disk .rk,.risk.throttle .rk,.risk.sev3 .rk,.risk.disk .ic,.risk.throttle .ic,.risk.sev3 .ic{color:var(--downf)}
 .risk.sd-wear,.risk.sev2{border-left-color:var(--warn)}
 .risk.sd-wear .rk,.risk.sev2 .rk,.risk.sd-wear .ic,.risk.sev2 .ic{color:var(--warnf)}
 .risk.sev1{border-left-color:var(--accent)}.risk.sev1 .rk{color:var(--accent)}
 /* ---- services ---- */
 .svc-tbl{border-collapse:separate;border-spacing:0;width:100%;font-size:12.5px;
   background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;margin-bottom:6px}
 .svc-tbl th{background:var(--bg2);color:var(--mut);font-weight:600;text-transform:uppercase;font-size:10.5px;
   letter-spacing:.5px;text-align:right;padding:9px 12px;border-bottom:1px solid var(--line2);white-space:nowrap}
 .svc-tbl th:first-child,.svc-tbl th:nth-child(2){text-align:left}
 .svc-tbl td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;
   font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--fg)}
 .svc-tbl td.tname{text-align:left;font-family:var(--sans);font-weight:500}
 .svc-tbl tbody tr:last-child td{border-bottom:none}
 .svc-tbl tbody tr{cursor:pointer}.svc-tbl tbody tr:hover{background:var(--card2)}
 .badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:20px;
   font:600 10px var(--sans);text-transform:uppercase;letter-spacing:.3px}
 .badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
 .badge.ok{background:var(--ok-bg);color:var(--okf)}.badge.bad{background:var(--down-bg);color:var(--downf)}
 .badge.warn{background:var(--warn-bg);color:var(--warnf)}
 /* ---- cost ---- */
 #cost{max-width:1040px}
 .fnote{color:var(--dim);font-size:12px;margin-bottom:16px;line-height:1.5}
 .frow{display:flex;align-items:center;gap:12px;padding:7px 0}
 .fname{width:140px;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;
   font-weight:500;font-size:12.5px}
 .fbar{flex:1 1 auto;height:18px;background:var(--card);border:1px solid var(--line);border-radius:6px;
   overflow:hidden;max-width:520px;min-width:70px}
 .ffill{height:100%;border-radius:5px;background:linear-gradient(90deg,var(--accent),#7c83ff);
   box-shadow:0 0 14px rgba(88,166,255,.3);transition:width .5s ease}
 .fval{flex:0 0 auto;width:96px;text-align:right;font:600 12.5px var(--mono);font-variant-numeric:tabular-nums}
 .frpd{flex:0 0 auto;width:128px;text-align:right;color:var(--mut);font-size:11.5px;font-family:var(--mono)}
 .ftop{flex:0 0 auto;width:160px;color:var(--dim);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 @media(max-width:680px){.frpd,.ftop{display:none}}
 /* ---- detail modal ---- */
 #detail{position:fixed;inset:0;background:rgba(4,6,10,.74);backdrop-filter:blur(4px);
   -webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:18px;
   z-index:50;animation:fade .18s ease}
 #detail[hidden]{display:none}
 .dwin{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line2);
   border-radius:14px;width:min(98vw,1700px);max-height:94vh;display:flex;flex-direction:column;
   overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.6)}
 .dbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:11px 14px;border-bottom:1px solid var(--line)}
 .dbar .nm{font-weight:700;font-size:15px;display:flex;align-items:center;gap:8px}
 .dh{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
 .dh.sep{padding-left:11px;margin-left:3px;border-left:1px solid var(--line2)}
 .dh button,#dclose{background:var(--card);border:1px solid var(--line2);color:var(--mut);
   border-radius:7px;padding:4px 10px;font:500 12px var(--sans);cursor:pointer;transition:.12s}
 .dh button:hover{color:var(--fg);border-color:var(--accent)}
 #dpanels button{padding:3px 8px;font-size:11px}
 .dh button.on{border-color:var(--accent);color:var(--accent);background:var(--accent-bg)}
 #dclose{margin-left:auto;font-weight:700;width:30px;height:30px;padding:0;display:flex;
   align-items:center;justify-content:center;border-radius:8px}
 #dclose:hover{color:var(--downf);border-color:var(--down)}
 .dimg{overflow:auto;background:var(--bg);min-height:120px}
 #dwrap{position:relative;width:100%}
 #dwrap img{display:block;width:100%;height:auto}
 #dover{position:absolute;inset:0}
 #dover .p{position:absolute;cursor:help}
 #dmsg{padding:40px;color:var(--dim);text-align:center;font-style:italic}
 #dplot{margin:0;padding:10px 12px;background:var(--bg);color:var(--fg);overflow-y:auto;overflow-x:hidden;
        height:80vh;font:12px/1.05 var(--mono);white-space:pre}
 #dplot[hidden]{display:none}
 /* braille glyphs (plotext markers) come from a fallback font that is wider than the mono
    cell, which drifts every data row out of line with the ascii axes. --brls is measured at
    render time (mono cell minus braille cell, so negative) to pull each braille char back to
    exactly one cell -> the curve lines up again. */
 #dplot .br{letter-spacing:var(--brls,0px)}
 .dfoot{padding:9px 14px;border-top:1px solid var(--line);color:var(--dim);font-size:11.5px;font-family:var(--mono)}
</style></head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
 <linearGradient id="gOk" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3fb950" stop-opacity=".5"/><stop offset="1" stop-color="#3fb950" stop-opacity="0"/></linearGradient>
 <linearGradient id="gWarn" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e3b341" stop-opacity=".5"/><stop offset="1" stop-color="#e3b341" stop-opacity="0"/></linearGradient>
 <linearGradient id="gDown" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f85149" stop-opacity=".5"/><stop offset="1" stop-color="#f85149" stop-opacity="0"/></linearGradient>
 <linearGradient id="gIngest" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7c83ff" stop-opacity=".5"/><stop offset="1" stop-color="#7c83ff" stop-opacity="0"/></linearGradient>
</defs></svg>
<header>
 <div class="hrow">
  <div class="brand">
   <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h3.5l2-7 4 15 3-10 1.5 3H22"/></svg>
   <h1>smokemon <b>FLEET</b></h1>
  </div>
  <div class="tabs" id="tabs"></div>
  <span class="meta" id="meta">connecting…</span>
 </div>
 <div class="hrow2">
  <div class="healthband">
   <span class="hb-label">fleet health</span>
   <div class="hb-track" id="hbtrack">
    <span class="hb-seg healthy" id="hb-healthy"></span>
    <span class="hb-seg warn" id="hb-warn"></span>
    <span class="hb-seg down" id="hb-down"></span>
    <span class="hb-seg stale" id="hb-stale"></span>
   </div>
  </div>
  <div class="pills" id="pills"></div>
  <input id="q" placeholder="filter nodes…" autocomplete="off">
 </div>
</header>
<div id="err"></div>
<div id="grid" class="view"></div>
<div id="table" class="view" hidden></div>
<div id="rank" class="view" hidden></div>
<div id="heat" class="view" hidden></div>
<div id="risk" class="view" hidden></div>
<div id="svc" class="view" hidden></div>
<div id="cost" class="view" hidden></div>
<div id="detail" hidden>
 <div class="dwin">
  <div class="dbar">
   <span class="nm" id="dname"></span>
   <span class="dh" id="dmode"></span>
   <span class="dh" id="dhours"></span>
   <span class="dh" id="dcols"></span>
   <span class="dh sep" id="dpanels"></span>
   <button id="dclose">✕</button>
  </div>
  <div class="dimg" id="dimg"><div id="dwrap"><img id="dgraph" alt=""><div id="dover"></div></div><div id="dmsg" hidden>no data in this window</div></div>
  <pre id="dplot" hidden></pre>
  <div class="dfoot" id="dfoot"></div>
 </div>
</div>
<script>
const params=new URLSearchParams(location.search);
const REFRESH=Math.max(1,parseFloat(params.get("refresh"))||5)*1000;
const q=document.getElementById("q"),grid=document.getElementById("grid"),
 pills=document.getElementById("pills"),meta=document.getElementById("meta"),
 err=document.getElementById("err");
let last={nodes:[],counts:{}};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtRtt=r=>r==null?"--":Math.round(r)+"ms";
const fmtLoss=l=>l==null?"":"l"+Math.round(l)+"%";
function fmtAge(a){if(a==null)return"?";if(a<90)return a+"s";if(a<5400)return Math.round(a/60)+"m";return Math.round(a/3600)+"h";}
function fmtDur(s){if(!s)return"-";if(s<90)return Math.round(s)+"s";if(s<5400)return Math.round(s/60)+"m";if(s<172800)return(s/3600).toFixed(1)+"h";return Math.round(s/86400)+"d";}
function tago(ts){return fmtAge(Math.round(Date.now()/1000-ts))+" ago";}
let sparks={};
// inline RTT sparkline (last ~2h) as a gradient-filled area + line (vanilla SVG, no deps). the
// fill gradient (#gOk/#gWarn/#gDown) and stroke colour key off the latest value. every coordinate
// is a number we compute, so this never interpolates node-controlled strings into the markup.
function sparkArea(node,cls){
 const s=sparks[node];if(!s)return"";
 const pv=s.map((v,i)=>[i,v]).filter(p=>p[1]!=null);
 if(pv.length<2)return"";
 const xmax=s.length-1,ys=pv.map(p=>p[1]),lo=Math.min(...ys),hi=Math.max(...ys),rng=(hi-lo)||1;
 const X=p=>(p[0]/xmax*100).toFixed(1),Y=p=>(28-(p[1]-lo)/rng*26).toFixed(1);
 const pts=pv.map(p=>X(p)+" "+Y(p));
 const last=ys[ys.length-1],g=last>250?"gDown":last>120?"gWarn":"gOk",
  col=last>250?"#ff7b72":last>120?"#e3b341":"#56d364";
 return `<svg class="${cls||"spark"}" viewBox="0 0 100 30" preserveAspectRatio="none">`
  +`<title>rtt ${Math.round(last)}ms</title>`
  +`<path d="M${X(pv[0])},30 L${pts.join(" L")} L${X(pv[pv.length-1])},30 Z" fill="url(#${g})"/>`
  +`<path d="M${pts.join(" L")}" fill="none" stroke="${col}" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}
function render(){
 // header pills + health band always reflect the latest fleet-status counts (any active view)
 const c=last.counts||{},total=(last.nodes||[]).length;
 pills.innerHTML=[["healthy"],["warn"],["down"],["stale"]].map(([k])=>
  `<span class="pill ${k}"><span class="dot s-${k}"></span><span class="pc">${c[k]||0}</span><span class="pl">${k}</span></span>`).join("");
 ["healthy","warn","down","stale"].forEach(k=>{const el=document.getElementById("hb-"+k);
  if(el)el.style.width=(total?((c[k]||0)/total*100):0).toFixed(2)+"%";});
 if(view==="grid")renderGrid();
 else if(view==="table"){
  const term=q.value.trim().toLowerCase();
  renderTable((last.nodes||[]).filter(n=>!term||n.node.toLowerCase().includes(term)));
 }
}
// ---- fleet overview (grid tab): status donut + ingest area gauge + KPI cards ------------
// static skeleton (no dynamic data -> safe innerHTML once); all live values are written via
// textContent / setAttribute below so dynamic strings are never interpolated into markup.
const GRID_SKELETON=`<div class="ov"><div class="ov-hero">`
 +`<div class="card"><div class="card-h">fleet status</div><div class="donut-wrap">`
 +`<svg class="donut" viewBox="0 0 120 120">`
 +`<circle class="track" cx="60" cy="60" r="52"/>`
 +`<circle class="seg healthy" cx="60" cy="60" r="52" data-k="healthy"/>`
 +`<circle class="seg warn" cx="60" cy="60" r="52" data-k="warn"/>`
 +`<circle class="seg down" cx="60" cy="60" r="52" data-k="down"/>`
 +`<circle class="seg stale" cx="60" cy="60" r="52" data-k="stale"/>`
 +`<text class="donut-total" id="donut-total" x="60" y="57" text-anchor="middle">--</text>`
 +`<text class="donut-cap" x="60" y="74" text-anchor="middle">nodes</text></svg>`
 +`<div class="donut-legend">`
 +["healthy","warn","down","stale"].map(k=>`<div class="lg ${k}"><span class="dot s-${k}"></span><span class="lg-label">${k}</span><span class="lg-count" data-k="${k}">0</span></div>`).join("")
 +`</div></div></div>`
 +`<div class="card ingest-card"><div class="card-h">hub ingest<span class="card-sub">realtime throughput</span></div>`
 +`<div class="gauge-row"><span class="gval" id="ig-rate">--</span><span class="gunit">KB/s</span></div>`
 +`<svg class="igspark" id="ig-spark" viewBox="0 0 100 36" preserveAspectRatio="none">`
 +`<path id="ig-area" fill="url(#gIngest)"/>`
 +`<polyline id="ig-poly" fill="none" stroke="#7c83ff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
 +`<circle id="ig-dot" r="0" fill="#9ea4ff"/></svg>`
 +`<span class="gsub" id="ig-sub">waiting for ingest…</span></div>`
 +`</div><div class="kpis" id="agg-tiles"></div></div>`;
const GRID_TILES=[["nodes","nodes"],["rtt","avg rtt"],["loss","avg loss"],["cpu","avg cpu"],
 ["mem","avg mem"],["temp","max temp"],["ship","ship / day"],["rows","rows / day"]];
const DONUT_C=2*Math.PI*52;  // donut ring circumference (r=52)
let gridBuilt=false,igRate,igSub,igArea,igPoly,igDot,tileVals={},meterFills={},donutSegs={},donutTotal,legendCounts={};
function buildGrid(){
 grid.innerHTML=GRID_SKELETON;
 igRate=document.getElementById("ig-rate");igSub=document.getElementById("ig-sub");
 igArea=document.getElementById("ig-area");igPoly=document.getElementById("ig-poly");igDot=document.getElementById("ig-dot");
 donutTotal=document.getElementById("donut-total");donutSegs={};legendCounts={};
 grid.querySelectorAll(".donut .seg").forEach(c=>donutSegs[c.dataset.k]=c);
 grid.querySelectorAll(".lg-count").forEach(c=>legendCounts[c.dataset.k]=c);
 const tc=document.getElementById("agg-tiles");tileVals={};meterFills={};
 GRID_TILES.forEach(([k,label])=>{
  const card=document.createElement("div");card.className="kpi";
  const v=document.createElement("div");v.className="tv";v.textContent="--";
  const l=document.createElement("div");l.className="tl";l.textContent=label;
  card.appendChild(v);card.appendChild(l);
  if(k==="cpu"||k==="mem"){const m=document.createElement("div");m.className="meter";
   const f=document.createElement("div");f.className="meter-fill";m.appendChild(f);card.appendChild(m);meterFills[k]=f;}
  tc.appendChild(card);tileVals[k]=v;
 });
 gridBuilt=true;
}
function kpiTone(v,warn,bad){return v==null?"":v>=bad?"bad":v>=warn?"warn":"ok";}
function setKpi(k,text,tone){const v=tileVals[k];v.textContent=text;
 const card=v.parentElement;card.classList.remove("ok","warn","bad");if(tone)card.classList.add(tone);}
function renderGrid(){
 if(!gridBuilt)buildGrid();
 const ns=last.nodes||[],c=last.counts||{},total=ns.length;
 const num=a=>a.filter(v=>v!=null),avg=a=>a.length?a.reduce((s,v)=>s+v,0)/a.length:null;
 const rtts=num(ns.map(x=>x.rtt_ms)),losses=num(ns.map(x=>x.loss_pct)),
  cpus=num(ns.map(x=>x.cpu)),mems=num(ns.map(x=>x.mem)),temps=num(ns.map(x=>x.temp));
 // donut: stacked arcs via dasharray + offset (the css transition animates the change)
 donutTotal.textContent=total;let off=0;
 ["healthy","warn","down","stale"].forEach(k=>{const frac=total?((c[k]||0)/total):0,seg=donutSegs[k];
  if(seg){seg.setAttribute("stroke-dasharray",(frac*DONUT_C).toFixed(2)+" "+DONUT_C.toFixed(2));
   seg.setAttribute("stroke-dashoffset",(-off*DONUT_C).toFixed(2));}
  off+=frac;if(legendCounts[k])legendCounts[k].textContent=c[k]||0;});
 // kpi cards (values + semantic colour; cpu/mem also get a saturation meter)
 tileVals.nodes.textContent=total;
 setKpi("rtt",rtts.length?Math.round(avg(rtts))+"ms":"--",rtts.length?kpiTone(avg(rtts),120,250):"");
 setKpi("loss",losses.length?avg(losses).toFixed(1)+"%":"--",losses.length?kpiTone(avg(losses),1,5):"");
 setKpi("temp",temps.length?Math.round(Math.max(...temps))+"°":"--",temps.length?kpiTone(Math.max(...temps),70,80):"");
 const cpuA=cpus.length?avg(cpus):null,memA=mems.length?avg(mems):null;
 setKpi("cpu",cpuA==null?"--":Math.round(cpuA)+"%",cpuA==null?"":kpiTone(cpuA,70,90));
 setKpi("mem",memA==null?"--":Math.round(memA)+"%",memA==null?"":kpiTone(memA,75,90));
 if(meterFills.cpu)meterFills.cpu.style.width=(cpuA==null?0:Math.max(0,Math.min(100,cpuA)))+"%";
 if(meterFills.mem)meterFills.mem.style.width=(memA==null?0:Math.max(0,Math.min(100,memA)))+"%";
 const fv=Object.values(foot);
 if(fv.length){
  const sd=fv.reduce((s,x)=>s+(x.wire_bytes_per_day!=null?x.wire_bytes_per_day:(x.wire_bytes||0)),0);
  const rd=fv.reduce((s,x)=>s+(x.rows_per_day!=null?x.rows_per_day:(x.rows||0)),0);
  tileVals.ship.textContent=fmtKB(sd);tileVals.rows.textContent=fmtK(Math.round(rd));
 }else{tileVals.ship.textContent="--";tileVals.rows.textContent="--";}
 renderIngest();
}
// live ingest gauge: current KB/s + a 15-min wire-bytes area chart from /api/ingest-rate.
let ingest=null;
function renderIngest(){
 if(view!=="grid")return;
 if(!gridBuilt)buildGrid();
 const s=(ingest&&ingest.series_bytes)||[];
 if(!ingest||s.length<2){igRate.textContent="--";igSub.textContent="waiting for ingest…";
  igArea.setAttribute("d","");igPoly.setAttribute("points","");igDot.setAttribute("r","0");return;}
 const kbs=(ingest.bytes_per_s||0)/1024;
 igRate.textContent=kbs>=100?Math.round(kbs):kbs>=10?kbs.toFixed(1):kbs.toFixed(2);
 const mins=Math.round((ingest.window_s||900)/60);
 igSub.textContent=`${(ingest.rows_per_s||0).toFixed(1)} rows/s · ${ingest.posts||0} posts/${mins}m · last `
  +(ingest.last_ts?tago(ingest.last_ts):"never");
 const n=s.length-1,hi=Math.max(...s,1),X=i=>(i/n*100).toFixed(2),Y=v=>(34-(v/hi)*32).toFixed(2);
 const pts=s.map((v,i)=>X(i)+" "+Y(v));
 igArea.setAttribute("d","M0,36 L"+pts.join(" L")+" L100,36 Z");
 igPoly.setAttribute("points",s.map((v,i)=>X(i)+","+Y(v)).join(" "));
 const lv=s[s.length-1],live=Math.max(...s)>0;
 igDot.setAttribute("cx",X(n));igDot.setAttribute("cy",Y(lv));igDot.setAttribute("r",live?"2.4":"0");
 igPoly.setAttribute("stroke",live?"#7c83ff":"#566071");
}
async function ingestTick(){try{const r=await fetch("/api/ingest-rate",{cache:"no-store"});
 if(r.ok){ingest=await r.json();if(view==="grid")renderIngest();}}catch(e){}}
// per-node view (table tab): a sortable table with inline cpu/mem meters + RTT trend sparklines,
// or a status-tile grid (layout toggle). live off the fleet-status poll + the spark/cost caches;
// click a row/tile to open the graphs. node-controlled strings go through esc() before innerHTML.
let nodeLayout="table",tableSort={key:"",dir:-1};
function sortVal(n,k){
 if(k==="node")return n.node.toLowerCase();
 if(k==="state")return ({down:0,stale:1,warn:2,healthy:3})[n.state];
 if(k==="ship"){const f=foot[n.node]||{};return f.wire_bytes_per_day!=null?f.wire_bytes_per_day:(f.wire_bytes||0);}
 if(k==="rows"){const f=foot[n.node]||{};return f.rows_per_day!=null?f.rows_per_day:(f.rows||0);}
 return n[k];
}
function meterCell(v,warn,bad){if(v==null)return "--";
 const t=v>=bad?"bad":v>=warn?"warn":"",w=Math.max(0,Math.min(100,v));
 return `<span class="mini ${t}"><span class="mbar"><i style="width:${w.toFixed(0)}%"></i></span><b>${Math.round(v)}%</b></span>`;}
function tableHtml(nodes){
 const tcls=(v,warn,bad)=>v==null?"":v>=bad?"bad":v>=warn?"warnv":"";
 const cols=[["state",""],["node","node"],["rtt_ms","rtt"],["loss_pct","loss"],["cpu","cpu"],["mem","mem"],
  ["temp","temp"],["_trend","trend"],["age_s","seen"],["rows","rows/d"],["ship","ship/d"]];
 const head=cols.map(([k,l])=>{const sortable=k!=="_trend"&&k!=="state";
  const ar=tableSort.key===k?`<span class="ar">${tableSort.dir>0?"▲":"▼"}</span>`:"";
  return `<th${sortable?` data-sort="${k}"`:""}>${esc(l)} ${ar}</th>`;}).join("");
 const body=nodes.map(n=>{
  const f=foot[n.node]||{},sd=f.wire_bytes_per_day!=null?f.wire_bytes_per_day:f.wire_bytes;
  return `<tr class="${n.state}" data-node="${esc(n.node)}">`
   +`<td class="stcell"><span class="st s-${n.state}" title="${esc(n.state)}"></span></td>`
   +`<td class="tname">${esc(n.node)}</td>`
   +`<td class="${n.rtt_ms==null?"":n.rtt_ms>250?"bad":n.rtt_ms>120?"warnv":""}">${fmtRtt(n.rtt_ms)}</td>`
   +`<td class="${n.loss_pct!=null&&n.loss_pct>0?"bad":""}">${n.loss_pct==null?"--":Math.round(n.loss_pct)+"%"}</td>`
   +`<td>${meterCell(n.cpu,70,90)}</td>`
   +`<td>${meterCell(n.mem,75,90)}</td>`
   +`<td class="${tcls(n.temp,70,80)}">${n.temp==null?"--":Math.round(n.temp)+"°"}</td>`
   +`<td>${sparkArea(n.node)||"<span style='color:var(--dim)'>--</span>"}</td>`
   +`<td>${fmtAge(n.age_s)}</td>`
   +`<td>${fmtK(f.rows_per_day!=null?f.rows_per_day:f.rows)}</td>`
   +`<td>${sd==null?"--":fmtKB(sd)+(f.wire_bytes_per_day!=null?"/d":"")}</td></tr>`;
 }).join("");
 return `<table class="grid-t"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function tilesHtml(nodes){
 return `<div class="tilegrid">`+nodes.map(n=>{
  const rc=n.rtt_ms==null?"":n.rtt_ms>250?"bad":n.rtt_ms>120?"warnv":"";
  return `<div class="ntile ${n.state}" data-node="${esc(n.node)}">`
   +`<div class="ntile-h"><span class="st s-${n.state}"></span>`
   +`<span class="nm">${esc(n.node)}</span><span class="chip ${n.state}">${esc(n.state)}</span></div>`
   +(sparkArea(n.node,"ntile-spark")||`<div class="ntile-spark"></div>`)
   +`<div class="ntile-m">`
   +`<span class="${rc}">rtt <b>${fmtRtt(n.rtt_ms)}</b></span>`
   +`<span class="${n.loss_pct>0?"bad":""}">loss <b>${n.loss_pct==null?"--":Math.round(n.loss_pct)+"%"}</b></span>`
   +`<span>cpu <b>${n.cpu==null?"--":Math.round(n.cpu)+"%"}</b></span>`
   +`<span>mem <b>${n.mem==null?"--":Math.round(n.mem)+"%"}</b></span>`
   +(n.temp==null?"":`<span>temp <b>${Math.round(n.temp)}°</b></span>`)
   +`<span>seen <b>${fmtAge(n.age_s)}</b></span></div></div>`;}).join("")+`</div>`;
}
function renderTable(nodes){
 const T=viewEl("table");
 const bar=`<div class="nodebar"><div class="seg-ctl" id="layout-ctl">`
  +`<button data-l="table" class="${nodeLayout==="table"?"on":""}">table</button>`
  +`<button data-l="tiles" class="${nodeLayout==="tiles"?"on":""}">tiles</button></div>`
  +`<span class="cnt">${nodes.length} node${nodes.length===1?"":"s"}</span></div>`;
 if(!nodes.length){T.innerHTML=bar+`<div class="empty">no nodes reporting yet</div>`;return;}
 let sorted=nodes;
 if(tableSort.key){const k=tableSort.key,d=tableSort.dir;sorted=nodes.slice().sort((a,b)=>{
  const va=sortVal(a,k),vb=sortVal(b,k);
  if(va==null&&vb==null)return 0;if(va==null)return 1;if(vb==null)return -1;
  return (va<vb?-1:va>vb?1:0)*d;});}
 T.innerHTML=bar+(nodeLayout==="tiles"?tilesHtml(sorted):tableHtml(sorted));
}
async function tick(){
 try{
  const r=await fetch("/api/fleet-status",{cache:"no-store"});
  if(!r.ok)throw new Error("HTTP "+r.status);
  last=await r.json();err.textContent="";
  meta.textContent=`${last.nodes.length} nodes · updated ${new Date().toLocaleTimeString()} · refresh ${REFRESH/1000}s`;
  render();
 }catch(e){err.textContent="fetch error: "+e.message;}
}
q.addEventListener("input",render);

// ---- view tabs: grid (live) · ranking · heatmap · risks. Only the active non-grid view
// polls (slow, 15s); grid status + sparklines + header pills always refresh. -------------
const VIEWS=[["grid","grid"],["table","table"],["rank","ranking"],["heat","heatmap"],["risk","risks"],["svc","services"],["cost","cost"]];
let view="grid",heatMetric="loss",heatHours=24;
// measured ship-cost cache (/api/cost): actual compressed bytes each node pushed over the wire,
// per node. shared by cost view, ranking columns and the modal stat line. cached ~25s.
let foot={},footTs=0;
function fmtKB(b){if(b==null)return"?";const u=["B","KB","MB","GB","TB"];let v=b,i=0;while(v>=1024&&i<u.length-1){v/=1024;i++;}return(i?v.toFixed(1):v.toFixed(0))+" "+u[i];}
function fmtK(n){return n==null?"--":n>=1000?(n/1000).toFixed(0)+"k":""+n;}
async function loadFoot(force){
 if(!force&&Object.keys(foot).length&&Date.now()-footTs<25000)return;
 try{const r=await fetch("/api/cost?hours=24",{cache:"no-store"});if(!r.ok)return;
  const d=await r.json();foot={};(d.nodes||[]).forEach(x=>foot[x.node]=x);footTs=Date.now();}catch(e){}
}
const tabs=document.getElementById("tabs"),viewEl=id=>document.getElementById(id);
tabs.innerHTML=VIEWS.map(([id,l])=>`<div class="tab" data-v="${id}">${l}</div>`).join("");
function setView(v){view=v;
 VIEWS.forEach(([id])=>viewEl(id).hidden=(id!==v));
 tabs.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.v===v));
 q.style.display=(v==="table")?"":"none";  // filter only applies to the per-node table now
 refreshView();}
function refreshView(){if(view==="rank")loadRank();else if(view==="heat")loadHeat();else if(view==="risk")loadRisk();else if(view==="cost")loadCost();else if(view==="svc")loadServices();
 else if(view==="table"){render();loadFoot().then(render);}
 else if(view==="grid"){render();ingestTick();loadFoot().then(()=>{if(view==="grid")renderGrid();});}}
tabs.addEventListener("click",e=>{if(e.target.dataset.v)setView(e.target.dataset.v);});

// open a node's graph modal from any view's [data-node] row
function nodeClick(box){box.addEventListener("click",e=>{const el=e.target.closest("[data-node]");if(el&&el.dataset.node)openDetail(el.dataset.node);});}

// ranking table (/api/fleet): uptime%, rtt, incidents, downtime - worst-first from server.
// footprint columns (rows/day, ship/day) joined in from the measured /api/cost cache.
async function loadRank(){await loadFoot();try{const r=await fetch("/api/fleet?hours=24",{cache:"no-store"});if(!r.ok)return;
 const rows=(await r.json()).fleet||[];
 const body=rows.map(x=>{const f=foot[x.node]||{},sd=f.wire_bytes_per_day!=null?f.wire_bytes_per_day:f.wire_bytes;
  const up=x.uptime_pct,upCls=up==null?"":up>=99.5?"okv":up>=95?"warnv":"bad";
  return `<tr data-node="${esc(x.node)}"><td class="tname">${esc(x.node)}</td>`
   +`<td class="${upCls}">${up==null?"--":up.toFixed(1)+"%"}</td>`
   +`<td class="${x.rtt_ms>250?"bad":x.rtt_ms>120?"warnv":""}">${fmtRtt(x.rtt_ms)}</td>`
   +`<td class="${x.incidents?"warnv":""}">${x.incidents}</td>`
   +`<td class="${x.downtime_s?"bad":""}">${fmtDur(x.downtime_s)}</td>`
   +`<td>${fmtK(f.rows_per_day!=null?f.rows_per_day:f.rows)}</td>`
   +`<td>${sd==null?"--":fmtKB(sd)}</td></tr>`;}).join("");
 viewEl("rank").innerHTML=rows.length?`<table class="grid-t"><thead><tr><th>node</th><th>uptime</th><th>rtt</th><th>incidents</th><th>downtime</th><th>rows/day</th><th>ship/day</th></tr></thead><tbody>${body}</tbody></table>`:`<div class="empty">no ranking data in this window</div>`;
}catch(e){}}

// cost view: horizontal bars comparing MEASURED ship volume (actual gzip bytes on the wire) per
// node, with the per-table breakdown so wasteful shippers stand out.
async function loadCost(){await loadFoot(true);renderCost();}
function renderCost(){const ns=Object.values(foot);
 if(!ns.length){viewEl("cost").innerHTML=`<div class="empty">no ship traffic measured yet (accrues from hub start)</div>`;return;}
 const val=x=>x.wire_bytes_per_day!=null?x.wire_bytes_per_day:(x.wire_bytes||0);
 ns.sort((a,b)=>val(b)-val(a));
 const max=Math.max(...ns.map(val))||1;
 const bars=ns.map(x=>{const v=val(x),pd=x.wire_bytes_per_day!=null;
  const top=x.top&&x.top.length?x.top.map(t=>t.t.replace("_samples","")).join(", "):"";
  return `<div class="frow" data-node="${esc(x.node)}" title="${x.posts} posts · observed ${fmtDur(x.observed_s)} · gzip ${x.ratio?x.ratio+":1":"?"} · raw ${fmtKB(x.raw_bytes)}"><span class="fname">${esc(x.node)}</span><div class="fbar"><div class="ffill" style="width:${(100*v/max).toFixed(1)}%"></div></div><span class="fval">${fmtKB(v)}${pd?"/day":""}</span><span class="frpd">${fmtK(x.rows_per_day!=null?x.rows_per_day:x.rows)} rows${pd?"/day":""}</span><span class="ftop">${esc(top)}</span></div>`;}).join("");
 viewEl("cost").innerHTML=`<div class="fnote">actual compressed bytes shipped to the hub per node (gzip on the wire, measured from POST sizes, ~24h). top tables show where the volume goes.</div>${bars}`;
}

// heatmap (/api/heatmap): node×hour grid, metric+window switchable. a smooth interpolated colour
// scale (calmer = cooler) plus a legend gradient and an hour axis, all derived from real values.
const HEAT_STOPS={loss:[[0,"#0e2a1a"],[1,"#1f6f3a"],[5,"#caa21a"],[25,"#d2691e"],[100,"#e5484d"]],
 rtt:[[0,"#0e2a1a"],[50,"#1f6f3a"],[120,"#caa21a"],[250,"#d2691e"],[600,"#e5484d"]]};
function hexRgb(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}
function heatColor(v){if(v==null)return "var(--card2)";
 const st=HEAT_STOPS[heatMetric]||HEAT_STOPS.loss;
 if(v<=st[0][0])return st[0][1];
 for(let i=1;i<st.length;i++){if(v<=st[i][0]){const A=hexRgb(st[i-1][1]),B=hexRgb(st[i][1]),
   t=(v-st[i-1][0])/((st[i][0]-st[i-1][0])||1);
  return `rgb(${Math.round(A[0]+(B[0]-A[0])*t)},${Math.round(A[1]+(B[1]-A[1])*t)},${Math.round(A[2]+(B[2]-A[2])*t)})`;}}
 return st[st.length-1][1];}
function heatGradientCss(){const st=HEAT_STOPS[heatMetric]||HEAT_STOPS.loss,mx=st[st.length-1][0];
 return "linear-gradient(90deg,"+st.map(s=>s[1]+" "+Math.round(s[0]/mx*100)+"%").join(",")+")";}
function heatTip(n,ts,v){const t=new Date(ts*1000),hh=String(t.getHours()).padStart(2,"0");
 const val=v==null?"no data":(heatMetric==="loss"?v+"% loss":v+" ms");
 return esc(n+"  "+hh+":00  "+val);}
async function loadHeat(){try{const r=await fetch(`/api/heatmap?metric=${heatMetric}&hours=${heatHours}`,{cache:"no-store"});if(!r.ok)return;
 const d=await r.json(),ns=Object.keys(d.nodes).sort(),hrs=d.hours||[];
 const tools=`<div class="heat-tools">`
  +`<div class="btn-grp"><button data-m="loss" class="${heatMetric==="loss"?"on":""}">loss %</button><button data-m="rtt" class="${heatMetric==="rtt"?"on":""}">rtt</button></div>`
  +`<div class="btn-grp">${[[6,"6h"],[24,"24h"],[168,"7d"]].map(([h,l])=>`<button data-hh="${h}" class="${heatHours===h?"on":""}">${l}</button>`).join("")}</div>`
  +`<div class="heat-legend"><span>${heatMetric==="loss"?"0%":"0ms"}</span><span class="bar" style="background:${heatGradientCss()}"></span><span>${heatMetric==="loss"?"100%":"600ms+"}</span></div></div>`;
 if(!ns.length){viewEl("heat").innerHTML=tools+`<div class="empty">no ping history in this window</div>`;return;}
 const rows=ns.map(n=>`<div class="hrow"><span class="hname" data-node="${esc(n)}">${esc(n)}</span><div class="hcells">`
  +d.nodes[n].map((v,i)=>`<div class="hcell" style="background:${heatColor(v)}" title="${heatTip(n,hrs[i],v)}"></div>`).join("")
  +`</div></div>`).join("");
 const axis=`<div class="haxis">`+hrs.map((ts,i)=>`<span>${i%6===0?new Date(ts*1000).getHours():""}</span>`).join("")+`</div>`;
 viewEl("heat").innerHTML=tools+`<div class="heatgrid">`+rows+axis+`</div>`;
}catch(e){}}
viewEl("heat").addEventListener("click",e=>{const t=e.target;
 if(t.dataset.m){heatMetric=t.dataset.m;loadHeat();}else if(t.dataset.hh){heatHours=+t.dataset.hh;loadHeat();}});

// risks (/api/risks): death-clocks (disk-full / SD-wear / throttle) + recent incident feed,
// as colour-coded cards with a per-kind glyph and the ETA / age emphasised on the right.
const RISK_ICON={
 disk:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>`,
 "sd-wear":`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 3h9l4 4v14H6z"/><path d="M10 3v4M13 3v4M16 7v3"/></svg>`,
 throttle:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13V5a2 2 0 0 1 4 0v8a4 4 0 1 1-4 0z"/></svg>`};
function riskIcon(k){return RISK_ICON[k]||RISK_ICON.disk;}
async function loadRisk(){try{const r=await fetch("/api/risks?hours=24",{cache:"no-store"});if(!r.ok)return;
 const d=await r.json(),cl=d.clocks||[],inc=d.incidents||[];
 const clh=cl.length?cl.map(c=>{const eta=c.eta_s==null?"":fmtDur(c.eta_s);
  return `<div class="risk ${esc(c.kind)}" data-node="${esc(c.node)}"><span class="ic">${riskIcon(c.kind)}</span>`
   +`<span class="rk">${esc(c.kind)}</span><span class="rn">${esc(c.node)}</span>`
   +`<span class="rd">${esc(c.detail)}</span>${eta?`<span class="reta">${esc(eta)}</span>`:""}</div>`;}).join(""):`<div class="empty">nothing projected to fail soon</div>`;
 const ih=inc.length?inc.map(i=>`<div class="risk sev${i.severity}" data-node="${esc(i.node)}">`
  +`<span class="rk">${esc(i.klass)}</span><span class="rn">${esc(i.node)}</span>`
  +`<span class="rd">${esc(i.scope)} · ${esc(i.detail)}</span><span class="reta">${esc(tago(i.start))}</span></div>`).join(""):`<div class="empty">no incidents in window</div>`;
 viewEl("risk").innerHTML=`<h2>death clocks <span class="cnt">${cl.length}</span></h2>${clh}<h2>recent incidents <span class="cnt">${inc.length}</span></h2>${ih}`;
}catch(e){}}
// services (/api/services): fleet-wide latest docker / redis / pipeline telemetry as tables.
// rows are click-through to the node graph modal (which has the matching time-series panels).
async function loadServices(){try{const r=await fetch("/api/services",{cache:"no-store"});if(!r.ok)return;
 renderServices(await r.json());}catch(e){}}
function renderServices(d){
 const S=viewEl("svc"),mb=v=>v==null?"--":Math.round(v)+"MB",pc=v=>v==null?"--":Math.round(v)+"%",
  agec=a=>a==null?"?":fmtAge(a),sec=(l,n)=>`<h2>${l}<span class="cnt">${n}</span></h2>`;
 let html="";
 const dk=d.docker||[],ddown=d.docker_down||[];
 if(dk.length||ddown.length){
  const body=dk.map(c=>{const st=c.bad?"bad":(c.running?"ok":"warn");
   const hl=c.health?` <span class="badge ${c.health==="unhealthy"?"bad":c.health==="healthy"?"ok":"warn"}">${esc(c.health)}</span>`:"";
   return `<tr data-node="${esc(c.node)}"><td class="tname">${esc(c.node)}</td><td class="tname">${esc(c.name)}</td>`
    +`<td style="text-align:left"><span class="badge ${st}">${esc(c.state||(c.running?"running":"stopped"))}</span>${hl}</td>`
    +`<td class="${c.cpu_pct>80?"bad":c.cpu_pct>50?"warnv":""}">${pc(c.cpu_pct)}</td><td>${mb(c.mem_mb)}</td>`
    +`<td class="${c.restart_count?"warnv":""}">${c.restart_count==null?"--":c.restart_count}</td>`
    +`<td class="${c.oom_killed?"bad":""}">${c.oom_killed?"OOM":""}</td><td>${agec(c.age_s)}</td></tr>`;}).join("");
  const dn=ddown.length?`<tr><td colspan="8" class="bad">daemon unreachable: ${esc(ddown.join(", "))}</td></tr>`:"";
  html+=sec("docker containers",dk.length)+`<table class="svc-tbl"><thead><tr><th>node</th><th>container</th><th>state</th><th>cpu</th><th>mem</th><th>restarts</th><th>oom</th><th>seen</th></tr></thead><tbody>${body}${dn}</tbody></table>`;
 }
 const rd=d.redis||[];
 if(rd.length){
  const body=rd.map(x=>{const up=(x.connected||0)>=1;
   const streams=(x.streams||[]).map(s=>`${esc(String(s.stream).split(":").pop())} ${s.xlen==null?0:s.xlen}${s.pending?"/"+s.pending+"p":""}`).join(", ");
   return `<tr data-node="${esc(x.node)}"><td class="tname">${esc(x.node)}</td><td class="tname">${esc(x.instance||"redis")}</td>`
    +`<td style="text-align:left"><span class="badge ${up?"ok":"bad"}">${up?"up":"down"}</span></td><td>${mb(x.used_memory_mb)}</td>`
    +`<td>${x.connected_clients==null?"--":x.connected_clients}</td>`
    +`<td class="${x.blocked_clients?"warnv":""}">${x.blocked_clients==null?"--":x.blocked_clients}</td>`
    +`<td>${x.ops_per_sec==null?"--":Math.round(x.ops_per_sec)}</td>`
    +`<td class="${x.evicted_keys?"warnv":""}">${x.evicted_keys==null?"--":x.evicted_keys}</td>`
    +`<td style="text-align:left;color:var(--mut)">${esc(streams)}</td><td>${agec(x.age_s)}</td></tr>`;}).join("");
  html+=sec("redis",rd.length)+`<table class="svc-tbl"><thead><tr><th>node</th><th>instance</th><th>state</th><th>mem</th><th>clients</th><th>blocked</th><th>ops/s</th><th>evicted</th><th>top streams</th><th>seen</th></tr></thead><tbody>${body}</tbody></table>`;
 }
 const pr=d.procs||[];
 if(pr.length){
  const body=pr.map(p=>{const up=(p.count||0)>0;
   return `<tr data-node="${esc(p.node)}"><td class="tname">${esc(p.node)}</td><td class="tname">${esc(p.label)}</td>`
    +`<td style="text-align:left"><span class="badge ${up?"ok":"bad"}">${up?"x"+p.count:"down"}</span></td>`
    +`<td>${pc(p.cpu_pct)}</td><td>${mb(p.rss_mb)}</td><td>${p.uptime_s==null?"--":fmtDur(p.uptime_s)}</td>`
    +`<td class="${p.restarts?"warnv":""}">${p.restarts==null?"--":p.restarts}</td><td>${agec(p.age_s)}</td></tr>`;}).join("");
  html+=sec("watched processes",pr.length)+`<table class="svc-tbl"><thead><tr><th>node</th><th>process</th><th>state</th><th>cpu</th><th>rss</th><th>uptime</th><th>restarts</th><th>seen</th></tr></thead><tbody>${body}</tbody></table>`;
 }
 const st=d.streams||[];
 if(st.length){
  const body=st.map(s=>{const ok=!!s.ok;
   return `<tr data-node="${esc(s.node)}"><td class="tname">${esc(s.node)}</td><td class="tname">${esc(s.url)}</td>`
    +`<td style="text-align:left"><span class="badge ${ok?"ok":"bad"}">${ok?"serving":"down"}</span></td>`
    +`<td>${s.latency_ms==null?"--":Math.round(s.latency_ms)+"ms"}</td>`
    +`<td style="text-align:left;color:var(--mut)">${esc(s.status||"")}</td><td>${agec(s.age_s)}</td></tr>`;}).join("");
  html+=sec("stream probes",st.length)+`<table class="svc-tbl"><thead><tr><th>node</th><th>endpoint</th><th>state</th><th>latency</th><th>status</th><th>seen</th></tr></thead><tbody>${body}</tbody></table>`;
 }
 S.innerHTML=html||`<div class="empty">no docker / redis / pipeline telemetry reported yet</div>`;
}
[viewEl("table"),viewEl("rank"),viewEl("heat"),viewEl("risk"),viewEl("svc"),viewEl("cost")].forEach(nodeClick);
// per-node view: layout toggle (table/tiles) + click-to-sort column headers (re-renders in place).
viewEl("table").addEventListener("click",e=>{
 const lb=e.target.closest("[data-l]");if(lb){nodeLayout=lb.dataset.l;render();return;}
 const th=e.target.closest("th[data-sort]");if(th&&th.dataset.sort){const k=th.dataset.sort;
  if(tableSort.key===k)tableSort.dir*=-1;else{tableSort.key=k;tableSort.dir=(k==="node")?1:-1;}render();}});

// per-node detail: embed the live PNG (same renderer as `smoke png`), refreshed every 15s
// (matches the shipper cadence; data granularity is PING_INTERVAL=10s so no point going lower).
const detail=document.getElementById("detail"),dgraph=document.getElementById("dgraph"),
 dname=document.getElementById("dname"),dhours=document.getElementById("dhours"),
 dcols=document.getElementById("dcols"),dpanels=document.getElementById("dpanels"),
 dover=document.getElementById("dover"),dmsg=document.getElementById("dmsg"),
 dfoot=document.getElementById("dfoot"),dplot=document.getElementById("dplot"),
 dimg=document.getElementById("dimg"),dmode=document.getElementById("dmode");
// render mode: png (matplotlib image) or plot (the TUI's plotext braille graphs as ANSI text).
// plot (braille) is the default — the granular terminal-style graphs open first; png is opt-in.
let dMode="plot";
dmode.innerHTML=[["png","png"],["plot","plot"]].map(([m,l])=>`<button data-m2="${m}">${l}</button>`).join("");
// xterm 256-colour index -> rgb, for converting plotext's ANSI (only 0 + 38;5;N appear).
function xterm256(n){
 const base=[[0,0,0],[205,0,0],[0,205,0],[205,205,0],[0,0,238],[205,0,205],[0,205,205],[229,229,229],
  [127,127,127],[255,0,0],[0,255,0],[255,255,0],[92,92,255],[255,0,255],[0,255,255],[255,255,255]];
 if(n<16)return base[n];
 if(n<232){n-=16;const r=Math.floor(n/36),g=Math.floor(n/6)%6,b=n%6,f=v=>v?55+40*v:0;return [f(r),f(g),f(b)];}
 const v=8+10*(n-232);return [v,v,v];}
// parse plotext ANSI without a regex (a backslash in a regex string gets mangled twice -
// once by python's triple-quoted string, once by JS - producing an invalid pattern). Split on
// the ESC byte; each part after the first starts with "[<code>m" (an SGR), the rest is text.
// wb() escapes html and wraps each braille run (U+2800-U+28FF) in .br so the --brls
// letter-spacing realigns the wider braille glyphs to the mono cell (see the css note).
// The range is built via fromCharCode (not a \\u literal) for the same reason the parser
// below avoids regex backslashes: python's triple-quote would mangle them.
function ansiToHtml(raw){
 const ESC=String.fromCharCode(27),
  e2=t=>t.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])),
  BR=new RegExp("["+String.fromCharCode(0x2800)+"-"+String.fromCharCode(0x28ff)+"]+","g"),
  wb=t=>e2(t).replace(BR,m=>'<span class="br">'+m+"</span>");
 const parts=raw.split(ESC);let out=wb(parts[0]),open=false;
 for(let i=1;i<parts.length;i++){
  const seg=parts[i],mi=seg.indexOf("m");
  if(seg[0]==="["&&mi!==-1){
   const code=seg.slice(1,mi),p=code.split(";");
   if(code===""||code==="0"){if(open){out+="</span>";open=false;}}
   else if(p[0]==="38"&&p[1]==="5"){const c=xterm256(+p[2]);if(open)out+="</span>";out+=`<span style="color:rgb(${c[0]},${c[1]},${c[2]})">`;open=true;}
   out+=wb(seg.slice(mi+1));
  }else{out+=wb(seg);}
 }
 if(open)out+="</span>";return out;}
// measure the actual monospace cell size so we can ask plotext for exactly the grid that fits
// the box (no overflow, no scroll). NL via fromCharCode: a literal newline in this string would
// be mangled by python's triple-quote + break the JS source.
let _cm=null;
function charMetrics(){
 if(_cm)return _cm;
 const NL=String.fromCharCode(10),BRC=String.fromCharCode(0x28ff),
  mk=txt=>{const s=document.createElement("span");
   s.style.cssText="position:absolute;visibility:hidden;white-space:pre;font:12px/1.05 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace";
   s.textContent=txt;document.body.appendChild(s);const r=s.getBoundingClientRect();document.body.removeChild(s);return r;};
 const r=mk(("0".repeat(100)+NL).repeat(10)),cw=(r.width/100)||7,bw=mk(BRC.repeat(100)).width/100;
 // bls: negative nudge that shrinks each (wider) braille glyph's advance back to one mono cell.
 _cm={cw,lh:(r.height/10)||13,bls:(bw?cw-bw:0)};return _cm;}
function renderFoot(){if(!dNode){dfoot.textContent="";return;}const f=foot[dNode];
 if(!f){dfoot.textContent="shipped: no measured traffic yet";return;}
 const sd=f.wire_bytes_per_day!=null?f.wire_bytes_per_day:f.wire_bytes,pd=f.wire_bytes_per_day!=null;
 dfoot.textContent=`shipped (measured ~24h): ${fmtKB(sd)}${pd?"/day":""} gzip · ${fmtK(f.rows_per_day!=null?f.rows_per_day:f.rows)} rows${pd?"/day":""}${f.ratio?" · "+f.ratio+":1 gzip":""}${f.top&&f.top.length?" · top: "+f.top.map(t=>t.t).join(", "):""}`;}
const HOURS=[[0.25,"15m"],[1,"1h"],[6,"6h"],[24,"24h"],[168,"7d"]],COLS=[[1,"1 col"],[2,"2 cols"],[3,"3 cols"]];
// dSel: Set of enabled panel keys, or null = "all". dAvail: keys that actually have data
// for this node (learned from the meta of the last full render), in render order.
let dNode=null,dH=0.25,dC=2,dTimer=null,dSel=null,dAvail=[];  // dH=0.25h = 15m default window
dhours.innerHTML=HOURS.map(([h,l])=>`<button data-h="${h}">${l}</button>`).join("");
dcols.innerHTML=COLS.map(([c,l])=>`<button data-c="${c}">${l}</button>`).join("");
function selParam(){if(!dSel)return"all";const on=dAvail.filter(k=>dSel.has(k));return on.length===dAvail.length?"all":on.join(",");}
function buildPanelButtons(){
 if(!dAvail.length){dpanels.innerHTML="";return;}
 const allOn=!dSel||dAvail.every(k=>dSel.has(k));
 dpanels.innerHTML=`<button data-p="*" class="${allOn?"on":""}">all</button>`+
  dAvail.map(k=>`<button data-p="${k}" class="${!dSel||dSel.has(k)?"on":""}">${k}</button>`).join("");
}
function pngSrc(){return `/api/png?node=${encodeURIComponent(dNode)}&hours=${dH}&cols=${dC}&panels=${selParam()}&_=${Date.now()}`;}
function decodeMeta(b64){try{return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(b64),c=>c.charCodeAt(0))));}catch(e){return [];}}
function freeBlob(){if(dgraph.src&&dgraph.src.startsWith("blob:"))URL.revokeObjectURL(dgraph.src);}
function showMsg(t){dmsg.textContent=t;dmsg.hidden=false;dover.innerHTML="";freeBlob();dgraph.removeAttribute("src");}
function syncCtl(){
 dhours.querySelectorAll("button").forEach(b=>b.classList.toggle("on",+b.dataset.h===dH));
 dcols.querySelectorAll("button").forEach(b=>b.classList.toggle("on",+b.dataset.c===dC));
 dmode.querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.m2===dMode));}
function paintActive(){return dMode==="plot"?paintPlot():paintGraph();}
async function paintGraph(){
 if(!dNode)return;
 syncCtl();
 if(dSel&&dSel.size===0){showMsg("select at least one panel");return;}
 const reqAll=selParam()==="all";
 try{
  const r=await fetch(pngSrc(),{cache:"no-store"});
  if(!r.ok){showMsg("no data in this window");return;}
  // titles live in this header now (not on the image); overlay them as hover tooltips,
  // positioned in % so the boxes scale with the image at any size.
  const panels=decodeMeta(r.headers.get("X-Smokemon-Panels")||"");
  // a full ("all") render is authoritative for which panels this node has -> rebuild filter.
  if(reqAll){dAvail=[...new Set(panels.map(p=>p.k).filter(Boolean))];if(!dSel)dSel=new Set(dAvail);buildPanelButtons();}
  const url=URL.createObjectURL(await r.blob());freeBlob();dgraph.src=url;dmsg.hidden=true;
  dover.innerHTML=panels.map(p=>`<div class="p" style="left:${(p.x*100).toFixed(2)}%;top:${(p.y*100).toFixed(2)}%;width:${(p.w*100).toFixed(2)}%;height:${(p.h*100).toFixed(2)}%" title="${esc(p.t)}"></div>`).join("");
 }catch(e){showMsg("fetch error");}
}
// plot mode: fetch the TUI's plotext frame (ANSI) sized to the modal and colourise it.
async function paintPlot(){
 if(!dNode)return;
 syncCtl();
 if(dSel&&dSel.size===0){dplot.textContent="select at least one panel";return;}
 // width fits the box exactly (no horizontal scroll). height gives each panel a readable ~16
 // lines: few panels fill the box, many panels grow taller and scroll vertically (frimärken
 // otherwise). single-panel via the filter = one big graph, no scroll.
 const cm=charMetrics(),aw=(dplot.clientWidth||1200)-20,ah=(dplot.clientHeight||640)-16;
 dplot.style.setProperty("--brls",cm.bls+"px");  // realign braille markers to the mono cell
 const w=Math.max(60,Math.min(400,Math.floor(aw/cm.cw)));
 const count=dSel?dSel.size:(dAvail.length||10),rows=Math.max(1,Math.ceil(count/dC));
 const h=Math.max(16,Math.min(300,Math.max(Math.floor(ah/cm.lh),rows*16)));
 try{
  const r=await fetch(`/api/plot?node=${encodeURIComponent(dNode)}&hours=${dH}&cols=${dC}&panels=${selParam()}&w=${w}&h=${h}&_=${Date.now()}`,{cache:"no-store"});
  if(!r.ok){dplot.textContent="no data in this window";return;}
  dplot.innerHTML=ansiToHtml(await r.text());
 }catch(e){dplot.textContent="fetch error";}
}
function setMode(m){dMode=m;dimg.hidden=(m!=="png");dplot.hidden=(m!=="plot");syncCtl();paintActive();}
function openDetail(node){dNode=node;detail.hidden=false;
 // name + a status dot looked up from the latest fleet-status, built via safe DOM (no innerHTML)
 dname.textContent="";const fn=(last.nodes||[]).find(x=>x.node===node);
 if(fn){const dot=document.createElement("span");dot.className="st s-"+fn.state;dname.appendChild(dot);}
 dname.appendChild(document.createTextNode(node));
 dSel=null;dAvail=[];dpanels.innerHTML="";  // reset filter; the first (all) render relearns this node's panels
 dfoot.textContent="";loadFoot().then(renderFoot);  // footprint stat line under the graphs
 dmsg.hidden=true;setMode(dMode);clearInterval(dTimer);dTimer=setInterval(paintActive,15000);}
function closeDetail(){detail.hidden=true;dNode=null;clearInterval(dTimer);dover.innerHTML="";freeBlob();dgraph.removeAttribute("src");}
grid.addEventListener("click",e=>{const n=e.target.closest(".node");if(n&&n.dataset.node)openDetail(n.dataset.node);});
dmode.addEventListener("click",e=>{if(e.target.dataset.m2)setMode(e.target.dataset.m2);});
dhours.addEventListener("click",e=>{if(e.target.dataset.h){dH=+e.target.dataset.h;paintActive();}});
dcols.addEventListener("click",e=>{if(e.target.dataset.c){dC=+e.target.dataset.c;paintActive();}});
// single-select: clicking a panel shows ONLY that one; "all" restores the full set.
dpanels.addEventListener("click",e=>{const k=e.target.dataset.p;if(!k)return;
 dSel=(k==="*")?new Set(dAvail):new Set([k]);
 buildPanelButtons();paintActive();});
document.getElementById("dclose").onclick=closeDetail;
detail.addEventListener("click",e=>{if(e.target===detail)closeDetail();});
addEventListener("keydown",e=>{if(e.key==="Escape")closeDetail();});

async function sparkTick(){try{const r=await fetch("/api/spark?hours=2",{cache:"no-store"});if(r.ok){sparks=(await r.json()).spark||{};if(view==="table")render();}}catch(e){}}
tick();setInterval(tick,REFRESH);
sparkTick();setInterval(sparkTick,30000);                 // sparklines: slow 2h trend (table)
ingestTick();setInterval(ingestTick,REFRESH);             // live ingest gauge (grid)
setInterval(()=>{if(view!=="grid")refreshView();},15000); // active non-grid view auto-refresh
setView("grid");
</script>
</body></html>
"""
