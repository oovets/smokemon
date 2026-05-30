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
 :root{--bg:#0b0e14;--fg:#c9d1d9;--mut:#6b7280;--card:#11151c;--line:#1c222c;
       --ok:#2ea043;--warn:#d29922;--down:#f85149;--stale:#484f58}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:13px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
        padding:10px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 h1{font-size:14px;margin:0;letter-spacing:.5px;font-weight:500}
 .pills{display:flex;gap:8px}
 .pill{padding:2px 9px;border-radius:10px;font-size:12px;display:flex;gap:6px;align-items:center}
 .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
 .s-healthy{background:var(--ok)}.s-warn{background:var(--warn)}
 .s-down{background:var(--down)}.s-stale{background:var(--stale)}
 .pill.healthy{background:#0f2417;color:#7ee2a8}.pill.warn{background:#241d0f;color:#e9c46a}
 .pill.down{background:#2a1314;color:#ff7b72}.pill.stale{background:#1a1d22;color:#8b949e}
 input{background:var(--card);border:1px solid var(--line);color:var(--fg);
       padding:5px 8px;border-radius:6px;font:inherit;min-width:160px}
 .meta{color:var(--mut);font-size:12px;margin-left:auto}
 #grid{padding:10px 12px;column-width:244px;column-gap:12px}
 .node{display:flex;align-items:center;gap:8px;padding:5px 9px;margin:0 0 6px;border-radius:6px;
       background:var(--card);border:1px solid var(--line);border-left:3px solid var(--stale);
       break-inside:avoid}
 .node.healthy{border-left-color:var(--ok)}.node.warn{border-left-color:var(--warn)}
 .node.down{border-left-color:var(--down)}.node.stale{border-left-color:var(--stale)}
 .node:hover{background:#161b24;border-color:#2a323d}
 .node.stale{color:var(--mut)}
 .name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .m{color:var(--mut);font-size:12px;flex:0 0 auto;min-width:44px;text-align:right}
 .m.bad{color:#ff7b72}
 #err{color:#ff7b72;padding:0 14px}
 .node{cursor:pointer}
 #detail{position:fixed;inset:0;background:rgba(0,0,0,.72);display:flex;
         align-items:center;justify-content:center;padding:16px;z-index:20}
 #detail[hidden]{display:none}
 .dwin{background:var(--card);border:1px solid var(--line);border-radius:8px;
       width:min(98vw,1700px);max-height:94vh;display:flex;flex-direction:column;overflow:hidden}
 .dbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid var(--line)}
 .dbar .nm{font-weight:600}
 .dh{display:flex;gap:6px;flex-wrap:wrap}
 .dh.sep{padding-left:10px;margin-left:2px;border-left:1px solid var(--line)}
 .dh button,#dclose{background:var(--bg);border:1px solid var(--line);color:var(--fg);
       border-radius:6px;padding:3px 9px;font:inherit;cursor:pointer}
 #dpanels button{padding:2px 7px;font-size:11px;color:var(--mut)}
 .dh button.on{border-color:var(--ok);color:#7ee2a8}
 #dclose{margin-left:auto;font-weight:700}
 .dimg{overflow:auto;background:var(--bg);min-height:120px}
 #dwrap{position:relative;width:100%}
 #dwrap img{display:block;width:100%;height:auto}
 #dover{position:absolute;inset:0}
 #dover .p{position:absolute;cursor:help}
 #dmsg{padding:28px;color:var(--mut)}
 #dplot{margin:0;padding:8px 10px;background:var(--card);color:#c9d1d9;overflow-y:auto;overflow-x:hidden;
        height:80vh;font:12px/1.05 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre}
 #dplot[hidden]{display:none}
 /* braille glyphs (plotext markers) come from a fallback font that is wider than the mono
    cell, which drifts every data row out of line with the ascii axes. --brls is measured at
    render time (mono cell minus braille cell, so negative) to pull each braille char back to
    exactly one cell -> the curve lines up again. */
 #dplot .br{letter-spacing:var(--brls,0px)}
 .tabs{display:flex;gap:4px}
 .tab{padding:3px 10px;border:1px solid var(--line);border-radius:6px;cursor:pointer;color:var(--mut);font-size:12px}
 .tab.on{border-color:var(--ok);color:#7ee2a8}
 .view[hidden]{display:none}
 #rank,#heat,#risk{padding:10px 14px}
 #heat{overflow-x:auto}
 .spark{flex:0 0 auto;width:50px;height:15px;opacity:.9}
 #rank table{border-collapse:collapse;width:100%;font-size:12px}
 #rank th,#rank td{padding:4px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
 #rank th:first-child,#rank td:first-child{text-align:left}
 #rank th{color:var(--mut);font-weight:500}
 #rank tbody tr{cursor:pointer}#rank tbody tr:hover{background:var(--card)}
 #table{padding:8px 12px;overflow-x:auto}
 #table table{border-collapse:collapse;width:100%;font-size:12px}
 #table th,#table td{padding:5px 11px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
 #table th:first-child,#table td:first-child,#table th:nth-child(2),#table td:nth-child(2){text-align:left}
 #table thead th{color:var(--mut);font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.4px}
 #table tbody tr{cursor:pointer}#table tbody tr:hover{background:#161b24}
 #table .st{width:9px;height:9px;border-radius:50%;display:inline-block;vertical-align:middle}
 #table td.bad{color:#ff7b72}#table td.warnv{color:#e9c46a}
 #table td .spark{vertical-align:middle}
 #table tr.stale td:not(:first-child):not(:nth-child(2)){color:var(--stale)}
 .hbar{display:flex;gap:14px;margin-bottom:10px}
 .hrow{display:flex;align-items:center;gap:8px;margin-bottom:2px}
 .hname{width:120px;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;font-size:12px}
 .hcells{display:flex;gap:1px}
 .hcell{width:13px;height:13px;border-radius:2px;flex:0 0 auto}
 .risk{display:flex;gap:10px;align-items:baseline;padding:3px 6px;border-radius:5px;cursor:pointer}
 .risk:hover{background:var(--card)}
 .rk{flex:0 0 auto;width:90px;color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
 .rn{flex:0 0 auto;width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .rd{color:var(--mut);font-size:12px}
 .risk.throttle .rk,.risk.disk .rk,.risk.sev3 .rk{color:#ff7b72}
 .risk.sd-wear .rk,.risk.sev2 .rk{color:#e9c46a}
 .view h2{font-size:12px;color:var(--mut);font-weight:500;letter-spacing:.5px;text-transform:uppercase;margin:14px 0 6px}
 .empty{color:var(--mut);padding:10px 4px}
 #cost{padding:10px 14px}
 .fnote{color:var(--mut);font-size:12px;margin-bottom:12px}
 .frow{display:flex;align-items:center;gap:10px;margin-bottom:4px}
 .fname{width:120px;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
 .fbar{flex:1 1 auto;height:14px;background:var(--line);border-radius:3px;overflow:hidden;max-width:460px}
 .ffill{height:100%;background:linear-gradient(90deg,#2ea043,#3fb950)}
 .fval{flex:0 0 auto;width:96px;text-align:right;font-size:12px}
 .frpd{flex:0 0 auto;width:120px;text-align:right;color:var(--mut);font-size:12px}
 .ftop{flex:0 0 auto;width:150px;color:var(--mut);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .dfoot{padding:6px 12px;border-top:1px solid var(--line);color:var(--mut);font-size:12px}
</style></head>
<body>
<header>
 <h1>smokemon <b>FLEET</b></h1>
 <div class="tabs" id="tabs"></div>
 <div class="pills" id="pills"></div>
 <input id="q" placeholder="filter nodes…" autocomplete="off">
 <span class="meta" id="meta">connecting…</span>
</header>
<div id="err"></div>
<div id="grid" class="view"></div>
<div id="table" class="view" hidden></div>
<div id="rank" class="view" hidden></div>
<div id="heat" class="view" hidden></div>
<div id="risk" class="view" hidden></div>
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
// inline RTT sparkline (last ~2h) as a tiny SVG polyline; no matplotlib, scales with the row.
function sparkSvg(node){
 const s=sparks[node];if(!s)return"";
 const pv=s.map((v,i)=>[i,v]).filter(p=>p[1]!=null);
 if(pv.length<2)return"";
 const xmax=s.length-1,ys=pv.map(p=>p[1]),lo=Math.min(...ys),hi=Math.max(...ys),rng=(hi-lo)||1;
 const pts=pv.map(p=>(p[0]/xmax*48+1).toFixed(1)+","+(14-(p[1]-lo)/rng*12).toFixed(1)).join(" ");
 const last=ys[ys.length-1],col=last>250?"#f85149":last>120?"#d29922":"#3fb950";
 return `<svg class="spark" viewBox="0 0 50 15" preserveAspectRatio="none" title="rtt ${Math.round(last)}ms"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1"/></svg>`;
}
function render(){
 const term=q.value.trim().toLowerCase();
 const nodes=last.nodes.filter(n=>!term||n.node.toLowerCase().includes(term));
 if(view==="table")renderTable(nodes);
 grid.innerHTML=nodes.map(n=>{
  const lossBad=n.loss_pct!=null&&n.loss_pct>0?" bad":"";
  const right=n.state==="stale"
   ?`<span class="m">${fmtAge(n.age_s)} ago</span>`
   :`<span class="m">${fmtRtt(n.rtt_ms)}</span><span class="m${lossBad}">${fmtLoss(n.loss_pct)}</span>`;
  return `<div class="node ${n.state}" data-node="${esc(n.node)}" title="${esc(n.node)} · cpu ${n.cpu??"?"}% · mem ${n.mem??"?"}% · ${n.temp??"?"}°C · ${fmtAge(n.age_s)} ago · click for graphs">`
   +`<span class="dot s-${n.state}"></span><span class="name">${esc(n.node)}</span>${sparkSvg(n.node)}${right}</div>`;
 }).join("");
 const c=last.counts||{};
 pills.innerHTML=[["healthy"],["warn"],["down"],["stale"]]
  .map(([k])=>`<span class="pill ${k}"><span class="dot s-${k}"></span>${c[k]||0}</span>`).join("");
}
// table view: one row per host with ALL details on a single line (worst-first, same order as the
// grid). live off the fleet-status poll + the spark/cost caches; click a row to open the graphs.
function renderTable(nodes){
 const T=viewEl("table");
 if(!nodes.length){T.innerHTML=`<div class="empty">no data</div>`;return;}
 const cls=(v,warn,bad)=>v==null?"":v>=bad?"bad":v>=warn?"warnv":"";
 const pct=v=>v==null?"--":Math.round(v)+"%";
 const body=nodes.map(n=>{
  const f=foot[n.node]||{},sd=f.wire_bytes_per_day!=null?f.wire_bytes_per_day:f.wire_bytes;
  const lossBad=n.loss_pct!=null&&n.loss_pct>0;
  const rttCls=n.rtt_ms==null?"":n.rtt_ms>250?"bad":n.rtt_ms>120?"warnv":"";
  return `<tr class="${n.state}" data-node="${esc(n.node)}">`
   +`<td><span class="st s-${n.state}"></span></td>`
   +`<td>${esc(n.node)}</td>`
   +`<td class="${rttCls}">${fmtRtt(n.rtt_ms)}</td>`
   +`<td class="${lossBad?"bad":""}">${n.loss_pct==null?"--":Math.round(n.loss_pct)+"%"}</td>`
   +`<td class="${cls(n.cpu,70,90)}">${pct(n.cpu)}</td>`
   +`<td class="${cls(n.mem,75,90)}">${pct(n.mem)}</td>`
   +`<td class="${cls(n.temp,70,80)}">${n.temp==null?"--":Math.round(n.temp)+"°"}</td>`
   +`<td>${sparkSvg(n.node)||"<span style='color:var(--mut)'>--</span>"}</td>`
   +`<td>${fmtAge(n.age_s)}</td>`
   +`<td>${fmtK(f.rows_per_day!=null?f.rows_per_day:f.rows)}</td>`
   +`<td>${sd==null?"--":fmtKB(sd)+(f.wire_bytes_per_day!=null?"/d":"")}</td>`
   +`</tr>`;
 }).join("");
 T.innerHTML=`<table><thead><tr><th></th><th>node</th><th>rtt</th><th>loss</th><th>cpu</th>`
  +`<th>mem</th><th>temp</th><th>trend</th><th>seen</th><th>rows/d</th><th>ship/d</th></tr></thead>`
  +`<tbody>${body}</tbody></table>`;
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
const VIEWS=[["grid","grid"],["table","table"],["rank","ranking"],["heat","heatmap"],["risk","risks"],["cost","cost"]];
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
 q.style.display=(v==="grid"||v==="table")?"":"none";
 refreshView();}
function refreshView(){if(view==="rank")loadRank();else if(view==="heat")loadHeat();else if(view==="risk")loadRisk();else if(view==="cost")loadCost();
 else if(view==="table"){render();loadFoot().then(render);}}
tabs.addEventListener("click",e=>{if(e.target.dataset.v)setView(e.target.dataset.v);});

// open a node's graph modal from any view's [data-node] row
function nodeClick(box){box.addEventListener("click",e=>{const el=e.target.closest("[data-node]");if(el&&el.dataset.node)openDetail(el.dataset.node);});}

// ranking table (/api/fleet): uptime%, rtt, incidents, downtime - worst-first from server.
// footprint columns (rows/day, ship/day) joined in from the measured /api/cost cache.
async function loadRank(){await loadFoot();try{const r=await fetch("/api/fleet?hours=24",{cache:"no-store"});if(!r.ok)return;
 const rows=(await r.json()).fleet||[];
 const body=rows.map(x=>{const f=foot[x.node]||{},sd=f.wire_bytes_per_day!=null?f.wire_bytes_per_day:f.wire_bytes;
  return `<tr data-node="${esc(x.node)}"><td>${esc(x.node)}</td><td>${x.uptime_pct==null?"--":x.uptime_pct.toFixed(1)}</td><td>${fmtRtt(x.rtt_ms)}</td><td>${x.incidents}</td><td>${fmtDur(x.downtime_s)}</td><td>${fmtK(f.rows_per_day!=null?f.rows_per_day:f.rows)}</td><td>${sd==null?"--":fmtKB(sd)}</td></tr>`;}).join("");
 viewEl("rank").innerHTML=rows.length?`<table><thead><tr><th>node</th><th>uptime%</th><th>rtt</th><th>incidents</th><th>downtime</th><th>rows/day</th><th>ship/day</th></tr></thead><tbody>${body}</tbody></table>`:`<div class="empty">no data</div>`;
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

// heatmap (/api/heatmap): node×hour grid, metric+window switchable.
function heatColor(v){if(v==null)return"#11151c";
 if(heatMetric==="loss")return v<=0?"#11251a":v<1?"#1f3a1f":v<5?"#5f5717":v<25?"#8a5a18":"#7a1f1f";
 return v<50?"#11251a":v<120?"#1f3a2a":v<250?"#5f5717":v<500?"#8a5a18":"#7a1f1f";}
async function loadHeat(){try{const r=await fetch(`/api/heatmap?metric=${heatMetric}&hours=${heatHours}`,{cache:"no-store"});if(!r.ok)return;
 const d=await r.json(),ns=Object.keys(d.nodes).sort();
 const ctl=`<div class="hbar"><span class="dh"><button data-m="loss" class="${heatMetric==="loss"?"on":""}">loss%</button><button data-m="rtt" class="${heatMetric==="rtt"?"on":""}">rtt</button></span><span class="dh">${[[6,"6h"],[24,"24h"],[168,"7d"]].map(([h,l])=>`<button data-hh="${h}" class="${heatHours===h?"on":""}">${l}</button>`).join("")}</span></div>`;
 const rows=ns.map(n=>`<div class="hrow"><span class="hname" data-node="${esc(n)}">${esc(n)}</span><div class="hcells">${d.nodes[n].map(v=>`<div class="hcell" style="background:${heatColor(v)}" title="${v==null?"no data":(heatMetric==="loss"?v+"% loss":v+" ms")}"></div>`).join("")}</div></div>`).join("");
 viewEl("heat").innerHTML=ctl+(ns.length?rows:`<div class="empty">no data</div>`);
}catch(e){}}
viewEl("heat").addEventListener("click",e=>{const t=e.target;
 if(t.dataset.m){heatMetric=t.dataset.m;loadHeat();}else if(t.dataset.hh){heatHours=+t.dataset.hh;loadHeat();}});

// risks (/api/risks): death-clocks (disk-full / SD-wear / throttle) + recent incident feed.
async function loadRisk(){try{const r=await fetch("/api/risks?hours=24",{cache:"no-store"});if(!r.ok)return;
 const d=await r.json(),cl=d.clocks||[],inc=d.incidents||[];
 const clh=cl.length?cl.map(c=>`<div class="risk ${c.kind}" data-node="${esc(c.node)}"><span class="rk">${esc(c.kind)}</span><span class="rn">${esc(c.node)}</span><span class="rd">${esc(c.detail)}</span></div>`).join(""):`<div class="empty">nothing projected to fail</div>`;
 const ih=inc.length?inc.map(i=>`<div class="risk sev${i.severity}" data-node="${esc(i.node)}"><span class="rk">${esc(i.klass)}</span><span class="rn">${esc(i.node)}</span><span class="rd">${esc(i.scope)} · ${esc(i.detail)} · ${tago(i.start)}</span></div>`).join(""):`<div class="empty">no incidents in window</div>`;
 viewEl("risk").innerHTML=`<h2>death clocks</h2>${clh}<h2>recent incidents</h2>${ih}`;
}catch(e){}}
[viewEl("table"),viewEl("rank"),viewEl("heat"),viewEl("risk"),viewEl("cost")].forEach(nodeClick);

// per-node detail: embed the live PNG (same renderer as `smoke png`), refreshed every 15s
// (matches the shipper cadence; data granularity is PING_INTERVAL=10s so no point going lower).
const detail=document.getElementById("detail"),dgraph=document.getElementById("dgraph"),
 dname=document.getElementById("dname"),dhours=document.getElementById("dhours"),
 dcols=document.getElementById("dcols"),dpanels=document.getElementById("dpanels"),
 dover=document.getElementById("dover"),dmsg=document.getElementById("dmsg"),
 dfoot=document.getElementById("dfoot"),dplot=document.getElementById("dplot"),
 dimg=document.getElementById("dimg"),dmode=document.getElementById("dmode");
// render mode: png (matplotlib image) or plot (the TUI's plotext braille graphs as ANSI text).
let dMode="png";
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
let dNode=null,dH=24,dC=2,dTimer=null,dSel=null,dAvail=[];
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
function openDetail(node){dNode=node;dname.textContent=node;detail.hidden=false;
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

async function sparkTick(){try{const r=await fetch("/api/spark?hours=2",{cache:"no-store"});if(r.ok){sparks=(await r.json()).spark||{};if(view==="grid"||view==="table")render();}}catch(e){}}
tick();setInterval(tick,REFRESH);
sparkTick();setInterval(sparkTick,30000);                 // sparklines: slow 2h trend
setInterval(()=>{if(view!=="grid")refreshView();},15000); // active non-grid view auto-refresh
setView("grid");
</script>
</body></html>
"""
