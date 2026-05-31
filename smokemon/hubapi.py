"""Read-only query layer behind the hub's GET endpoints: a Prometheus/OpenMetrics
exposition (S2) and a small JSON API with a fleet ranking and a node×hour heatmap
(S3). Pure stdlib, derives everything from the hub DB via direct SQL + the shared
analysis engine. Split out from hub.py so it can be unit-tested without a socket.

Latest-value queries lean on SQLite's documented bare-column behaviour: with a
MAX(ts) aggregate and GROUP BY node, the other selected columns come from the row
that holds that max ts - i.e. the most recent sample per node."""

import fnmatch
import sqlite3
import time

from . import analyze, config, core, duckio, mlanomaly, query, schema
from .probes.logexcerpt import is_elevated


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


def latest_metrics(conn, now: float | None = None) -> dict[str, dict]:
    """{node: {cpu, mem, temp, ts, targets:{name:{rtt,loss}}}} most-recent values.

    Bounded to the last HUB_LATEST_WINDOW_S so the per-group MAX(ts) seeks the recent tail of the
    (ts) index instead of scanning all history (the unbounded GROUP BY here was the hot path that
    made a large hub DB time out). A node silent longer than the window drops out of 'latest'.
    Window 0 = unbounded (legacy)."""
    now = time.time() if now is None else now
    win = config.HUB_LATEST_WINDOW_S
    floor = (now - win) if win > 0 else None
    where, params = (" WHERE ts >= ?", [floor]) if floor is not None else ("", [])
    out: dict[str, dict] = {}
    for node, cpu, mem, temp, ts in _rows(
            conn, "SELECT node, cpu_pct, mem_used_pct, temp_c, MAX(ts) FROM host_samples"
            + where + " GROUP BY node", params):
        out.setdefault(node, {})["cpu"] = cpu
        out[node]["mem"] = mem
        out[node]["temp"] = temp
        out[node]["ts"] = ts
    for node, target, rtt, loss, ts in _rows(
            conn, "SELECT node, target, rtt_median, loss_pct, MAX(ts) FROM ping_runs"
            + where + " GROUP BY node, target", params):
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
            _m = analyze._median(meds) if meds else None
            rtt_med = round(_m, 1) if _m is not None else None
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


def heatmap(conn, metric: str = "loss", hours: float = 24.0, until: float | None = None,
            duck=None) -> dict:
    """node × hour grid for 'loss' (max loss%) or 'rtt' (median RTT). Returns
    {metric, hours:[epoch...], nodes:{node:[val per hour]}}. When `duck` is a DuckDB connection
    (hub opt-in) the GROUP BY runs on the columnar engine for speed; otherwise it runs on the
    sqlite3 connection. Both paths use the same SQL and produce an identical result."""
    until = time.time() if until is None else until
    since = until - hours * 3600
    n_buckets = int(hours)
    hour0 = since - (since % 3600)
    col = "loss_pct" if metric == "loss" else "rtt_median"
    agg = "MAX" if metric == "loss" else "AVG"
    grid: dict[str, list] = {}
    sql = (f"SELECT node, CAST((ts - ?) / 3600 AS INT) hr, {agg}({col}) "
           "FROM ping_runs WHERE ts BETWEEN ? AND ? GROUP BY node, hr")
    params = (hour0, since, until)
    if duck is not None:
        try:
            rows = duckio.query_rows(duck, sql, params)
        except Exception as e:  # noqa: BLE001 - any duckdb hiccup degrades to sqlite, never 500s
            core.log(f"heatmap: duckdb query failed, using sqlite: {e!r}")
            rows = _rows(conn, sql, params)
    else:
        rows = _rows(conn, sql, params)
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
    latest = latest_metrics(conn, now)
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
    """Fleet-wide 'what's failing / about to fail', in three tiers:
      clocks    - predictive death-clock ETAs: disk-full, SD wear, memory exhaustion, thermal
      alerts    - current service/host degradations (see _service_alerts)
      incidents - recent network/HTTP incident feed (loss/latency/dns/http-error)
    Reuses the same detectors + ETA projections as `smoke incidents` / the PNG titles and the
    services view. On-demand (the risks tab fetches it), so per-node loads stay off the 5s path."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    # Only surface clocks that are actually actionable: a far-future projection (flat usage ->
    # huge ETA) is noise, not a death clock. Disk within a year, SD wear within five. Memory is
    # deliberately NOT a predictive clock (mem% hovers near 100 with cache -> too many false ETAs);
    # real memory trouble shows up as factual alerts instead (OOM kills / swap / PSI, see below).
    disk_horizon, wear_horizon = 365 * 86400, 5 * 365 * 86400
    clocks: list[dict] = []
    incidents: list[dict] = []
    anomalies: list[dict] = []
    for node in nodes(conn):
        ping = query.load_ping_agg(conn, since, now, None, node)
        http = query.load_http(conn, since, now, node)
        for inc in analyze.detect_incidents(ping, http):
            incidents.append({**inc, "node": node})
        # Multivariate anomalies: joint co-deviation across host/network signals that the
        # per-signal incident detectors miss. Reuses the analysis frame; numpy-optional.
        frame = analyze.build_frame(conn, since, now, node)
        for a in mlanomaly.multivariate_anomalies(frame):
            sigs = ", ".join(f"{name} {z:.0f}sigma" for name, z in a["signals"])
            anomalies.append({
                "node": node, "ts": a["ts"], "score": a["score"],
                "severity": 2 if a["score"] >= 6.0 else 1,
                "signals": a["signals"],
                "detail": f"{len(a['signals'])} signals co-deviated ({sigs})",
            })
        # drop read-only pseudo-mounts (snap/loop squashfs sit at 100% by design -> false "full")
        disk = {m: v for m, v in query.load_disk(conn, since, now, node).items()
                if not (m.startswith("/snap") or m.startswith("/var/snap") or "/snapd/" in m)}
        full = query.disk_full_eta(disk)
        if full and full[1] <= disk_horizon:
            eta = full[1]
            clocks.append({"node": node, "kind": "disk", "eta_s": round(eta),
                           "severity": 3 if eta <= 7 * 86400 else 2 if eta <= 30 * 86400 else 1,
                           "detail": f"{full[0]} full {query.human_eta(eta)}"})
        wear = query.wear_eta(query.load_disk_health(conn, since, now, node))
        if wear and wear[1] <= wear_horizon:
            eta = wear[1]
            clocks.append({"node": node, "kind": "sd-wear", "eta_s": round(eta),
                           "severity": 3 if eta <= 90 * 86400 else 2 if eta <= 365 * 86400 else 1,
                           "detail": f"{wear[0]} wear {query.human_eta(eta)}"})
        host = query.load_host(conn, since, now, node)
        if host:
            temp = query.last_value(host.get("temp", []))
            if temp is not None:
                head = config.THROTTLE_TEMP_C - temp
                if head <= 10.0:  # only surface when near the throttle ceiling
                    clocks.append({"node": node, "kind": "throttle", "eta_s": None,
                                   "severity": 3 if head <= 0 else 2 if head <= 5 else 1,
                                   "detail": f"{temp:.0f}C ({head:.0f}C to throttle)" if head > 0
                                   else f"{temp:.0f}C THROTTLING"})
    clocks.sort(key=lambda c: (c["eta_s"] is None, c["eta_s"] or 0))
    incidents += _http_error_incidents(conn, since, now)
    incidents.sort(key=lambda i: -i["start"])
    anomalies.sort(key=lambda a: -a["score"])
    return {"now": now, "hours": hours, "clocks": clocks,
            "alerts": _annotate_alerts(conn, _service_alerts(conn, hours, now), now),
            "incidents": incidents[:50],
            "incident_groups": _group_incidents(incidents),
            "anomalies": anomalies[:20]}


def _group_incidents(incidents: list[dict]) -> list[dict]:
    """Per-node correlated incident groups (storm dedup) for the risk feed. Incidents from
    different nodes are never grouped together; within a node, analyze.correlate_incidents
    folds a co-firing cluster into one group with a likely root. Each group carries its node
    and a one-line summary; raw members ride along so the dashboard can expand them."""
    by_node: dict[str, list[dict]] = {}
    for inc in incidents:
        by_node.setdefault(inc.get("node", ""), []).append(inc)
    out: list[dict] = []
    for node, incs in by_node.items():
        for g in analyze.correlate_incidents(incs):
            if len(g["members"]) < 2:
                continue  # singletons are already shown in the flat incident feed
            root = g["root"]
            kinds = ", ".join(sorted({m.get("klass", "?") for m in g["members"]}))
            out.append({
                "node": node, "start": g["start"], "end": g["end"],
                "severity": g["severity"], "klass": root.get("klass", "?"),
                "count": len(g["members"]),
                "detail": f"{len(g['members'])} correlated incidents ({kinds})",
                "members": g["members"],
            })
    out.sort(key=lambda g: -g["start"])
    return out


_ERROR_SEVS = {"error", "err", "crit", "critical", "fatal", "emerg", "alert", "panic"}
_LOG_PREVIEW = 2000  # chars of excerpt sent to the browser (the freshest tail)


def _event_rank(severity) -> int:
    """3 = error/crit, 2 = warn/other-elevated, 1 = info/quiet. Drives the row colour + filtering."""
    if (severity or "").strip().lower() in _ERROR_SEVS:
        return 3
    return 2 if is_elevated(severity) else 1


def events_log(conn, node=None, severity="elevated", hours: float = 24.0,
               limit: int = 200, now: float | None = None) -> dict:
    """Merged newest-first stream of ext_events + log_excerpts for the dashboard logs tab.
    `severity` filters the events: 'all' | 'elevated' (warn+; default) | 'error' (error/crit only).
    log_excerpts are incident tails (only captured when something elevated fired) and are included
    except in the strict 'error' view; their excerpt is tail-truncated for the browser."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    nf, npar = (" AND node=?", [node]) if node else ("", [])
    rows: list[dict] = []
    for ts, nd, src, sev, ev, det in conn.execute(
            "SELECT ts,node,source,severity,event,detail FROM ext_events "
            "WHERE ts>=?" + nf + " ORDER BY ts DESC LIMIT ?", [since, *npar, limit]):
        rank = _event_rank(sev)
        if severity == "elevated" and rank < 2:
            continue
        if severity == "error" and rank < 3:
            continue
        rows.append({"kind": "event", "ts": ts, "node": nd, "source": src or "",
                     "severity": sev or "", "label": ev or "", "detail": det or "", "sev": rank})
    if severity != "error":  # log tails ride along except in the strict error-only view
        for ts, nd, src, reason, nbytes, dropped, excerpt in conn.execute(
                "SELECT ts,node,source,reason,bytes,dropped,excerpt FROM log_excerpts "
                "WHERE ts>=?" + nf + " ORDER BY ts DESC LIMIT ?", [since, *npar, limit]):
            ex = excerpt or ""
            rows.append({"kind": "log", "ts": ts, "node": nd, "source": src or "", "severity": "",
                         "label": reason or "", "detail": ex[-_LOG_PREVIEW:], "sev": 2,
                         "bytes": nbytes, "dropped": dropped, "truncated": len(ex) > _LOG_PREVIEW})
    rows.sort(key=lambda r: -r["ts"])
    return {"now": now, "hours": hours, "node": node or "", "severity": severity, "rows": rows[:limit]}


def _num(x, suf="", pre=""):
    """Format a number as a display string (e.g. '512MB', '95%'), or None when not a number so
    _kv drops it. Keeps the alert 'extra' grid free of empty/None rows."""
    return f"{pre}{x:.0f}{suf}" if isinstance(x, (int, float)) else None


def _dur(x):
    """Compact human duration for an age/uptime in seconds, or None when missing/zero."""
    return query.human_eta(x) if isinstance(x, (int, float)) and x else None


def _kv(*pairs):
    """Build the alert 'extra' detail grid: [[label, value], ...] dropping empty values, so the
    modal only shows context fields that actually have data on this node."""
    return [[k, v] for k, v in pairs if v not in (None, "")]


def _service_alerts(conn, hours: float, now: float) -> list[dict]:
    """Current service/host degradations across the fleet (newest state per entity). Reuses
    services() for docker/redis/watched-procs/stream-probes, plus host-level OOM kills, swap /
    memory-pressure, CPU throttling and conntrack saturation.

    Each alert carries three text levels so the dashboard can show short -> full:
      summary - terse chip text (e.g. 'restart loop x5', 'OOM x3', 'conntrack 95%')
      detail  - the one-line headline (unchanged for back-compat; used as the chip tooltip)
      extra   - [[label, value], ...] context grid for the click-through modal, pulled from
                fields services()/the host+tcp SELECTs already fetch (zero extra edge cost)
    plus optional logs_hint (memory/throttle/oom): the modal then offers a Logs deep-link, since
    the literal kernel cause (e.g. the OOM victim) lives in log_excerpts, not the metric tables.
    Sorted most-severe first."""
    since = now - hours * 3600
    out: list[dict] = []

    def add(node, kind, sev, label, detail, summary, extra=None, logs_hint=False):
        a = {"node": node, "kind": kind, "severity": sev, "label": label,
             "detail": detail, "summary": summary, "extra": extra or []}
        if logs_hint:
            a["logs_hint"] = True
        out.append(a)

    svc = services(conn, hours, now)
    for n in svc.get("docker_down", []):
        add(n, "docker", 3, "daemon", "docker daemon unreachable", "daemon down")
    for c in svc.get("docker", []):
        # Only alert on containers that are SUPPOSED to be up but aren't working. A container that
        # has simply exited / been stopped (watchtower & other periodic jobs, portainer agents,
        # anything intentionally down) is NOT an alert -> this kills the false positives. We flag
        # crash loops (restarting), the stuck 'dead' state, live-but-failing healthchecks
        # (running+unhealthy), a live container that was OOM-killed, and heavy restart flapping.
        state, running = c.get("state"), c.get("running")
        rc = c.get("restart_count") or 0
        ex = _kv(("image", c.get("image")), ("cpu", _num(c.get("cpu_pct"), "%")),
                 ("mem", _num(c.get("mem_mb"), "MB")), ("pids", c.get("pids")),
                 ("restarts", rc or None), ("exit", c.get("exit_code")),
                 ("age", _dur(c.get("age_s"))))
        if state == "restarting":
            add(c["node"], "docker", 3, c["name"], "restart loop" + (f" ({rc}x)" if rc else ""),
                "restart loop" + (f" x{rc}" if rc else ""), ex, logs_hint=True)
        elif state == "dead":
            add(c["node"], "docker", 2, c["name"], "dead (stuck)", "dead", ex, logs_hint=True)
        elif running and c.get("health") == "unhealthy":
            add(c["node"], "docker", 2, c["name"], "unhealthy", "unhealthy", ex, logs_hint=True)
        elif running and c.get("oom_killed"):
            add(c["node"], "docker", 2, c["name"], "OOM-killed (restarted)", "OOM-killed", ex, logs_hint=True)
        elif running and rc >= 10:
            add(c["node"], "docker", 1, c["name"], f"{rc} restarts (flapping)", f"{rc} restarts", ex)
    for r in svc.get("redis", []):
        inst = r.get("instance") or "redis"
        rex = _kv(("mem", _num(r.get("used_memory_mb"), "MB")), ("clients", r.get("connected_clients")),
                  ("ops/s", _num(r.get("ops_per_sec"))), ("evicted", r.get("evicted_keys")))
        if (r.get("connected") or 0) < 1:
            add(r["node"], "redis", 3, inst, "instance down", "down", rex)
            continue
        # NB: blocked_clients>0 is normal (BLPOP/XREAD BLOCK consumers idle-wait) -> not an alert.
        if r.get("rejected_connections"):
            add(r["node"], "redis", 2, inst, f"{r['rejected_connections']} rejected connections",
                f"{r['rejected_connections']} rejected", rex)
        for s in r.get("streams", []):
            if (s.get("pending") or 0) >= 1000:
                add(r["node"], "stream", 2, str(s["stream"]).split(":")[-1],
                    f"{s['pending']} pending (xlen {s.get('xlen')})", f"pending {s['pending']}",
                    _kv(("xlen", s.get("xlen")), ("pending", s.get("pending"))))
    for p in svc.get("procs", []):
        if not p.get("count"):
            add(p["node"], "proc", 3, p["label"], "process missing", "gone",
                _kv(("last cpu", _num(p.get("cpu_pct"), "%")), ("rss", _num(p.get("rss_mb"), "MB")),
                    ("uptime", _dur(p.get("uptime_s"))), ("restarts", p.get("restarts") or None)))
    for s in svc.get("streams", []):
        if not s.get("ok"):
            add(s["node"], "stream", 2, query.host_label(s["url"]),
                f"probe failing (status {s.get('status')})", "probe failing",
                _kv(("url", s.get("url")), ("status", s.get("status")),
                    ("latency", _num(s.get("latency_ms"), "ms")), ("age", _dur(s.get("age_s")))))
    # host-level gauges: latest row per node (MAX(ts) bare-column trick, like services())
    for (node, oom, swap, psi_mem, thr_bits, thr_cnt, mem_pct, mem_total, cache, psi_io,
         temp, freq, cpu, load1, _ts) in _rows(
            conn, "SELECT node, oom_kill_count, swap_used_pct, psi_mem, pi_throttle_bits, "
            "cpu_throttle_count, mem_used_pct, mem_total_mb, cache_mb, psi_io, temp_c, "
            "cpu_freq_mhz, cpu_pct, load1, MAX(ts) FROM host_samples WHERE ts >= ? GROUP BY node", (since,)):
        if node is None:
            continue
        mem_ctx = _kv(("mem used", _num(mem_pct, "%")), ("total", _num(mem_total, "MB")),
                      ("cache", _num(cache, "MB")), ("swap", _num(swap, "%")),
                      ("psi mem", _num(psi_mem, "%")), ("psi io", _num(psi_io, "%")))
        if oom:
            add(node, "memory", 3, "oom-killer", f"{oom} OOM kills", f"OOM x{oom}",
                _kv(("kills", oom), *mem_ctx), logs_hint=True)
        if swap is not None and swap >= 80:
            add(node, "memory", 2, "swap", f"swap {swap:.0f}% used", f"swap {swap:.0f}%", mem_ctx, logs_hint=True)
        if psi_mem is not None and psi_mem >= 20:
            add(node, "memory", 2, "pressure", f"PSI mem {psi_mem:.0f}%", f"PSI mem {psi_mem:.0f}%",
                mem_ctx, logs_hint=True)
        if thr_bits or (thr_cnt or 0) > 0:
            add(node, "throttle", 2, "cpu", "throttling" + (f" ({thr_cnt}x)" if thr_cnt else ""),
                "throttling" + (f" x{thr_cnt}" if thr_cnt else ""),
                _kv(("temp", _num(temp, "C")), ("freq", _num(freq, "MHz")), ("cpu", _num(cpu, "%")),
                    ("load1", _num(load1)), ("count", thr_cnt or None)), logs_hint=True)
    for node, used, mx, retrans, rsts, estab, _ts in _rows(
            conn, "SELECT node, conntrack_used, conntrack_max, retrans_segs, out_rsts, "
            "estab_resets, MAX(ts) FROM tcp_samples WHERE ts >= ? GROUP BY node", (since,)):
        if node is None or not used or not mx:
            continue
        frac = used / mx
        if frac >= 0.8:
            add(node, "tcp", 3 if frac >= 0.95 else 2, "conntrack",
                f"{used}/{mx} ({frac * 100:.0f}%)", f"conntrack {frac * 100:.0f}%",
                _kv(("used", f"{used}/{mx}"), ("retrans", retrans), ("resets", rsts),
                    ("estab resets", estab)))
    out.sort(key=lambda a: (-a["severity"], a["kind"], a["node"]))
    return out


def _annotate_alerts(conn, alerts: list[dict], now: float) -> list[dict]:
    """Decorate each service alert (from _service_alerts) with delivery state for the Risk tab:
      muted    - matches a SMOKEMON_ALERT_MUTE glob (never paged, still shown here, dimmed)
      since_s  - how long it has been firing, from alert_state.first_ts (None until the alert
                 loop has recorded it; that loop only runs when a notify URL is configured)
      notified - whether a page has already been sent for it
    The key mirrors alerts._key ('node/kind/label'). Read-only and tolerant of the alert_state
    table being absent (older hub DB) - _rows swallows the OperationalError."""
    state = {r[0]: (r[1], r[2]) for r in _rows(
        conn, "SELECT key, first_ts, notified_ts FROM alert_state")}
    out = []
    for a in alerts:
        key = f"{a['node']}/{a['kind']}/{a.get('label', '')}"
        first_ts, notified_ts = state.get(key, (None, None))
        out.append({**a,
                    "muted": any(fnmatch.fnmatch(key, p) for p in config.ALERT_MUTE),
                    "since_s": round(now - first_ts) if first_ts is not None else None,
                    "notified": notified_ts is not None})
    return out


def _http_error_incidents(conn, since: float, now: float) -> list[dict]:
    """Recent HTTP failures (status >= 500, or 0 = request failed) folded into the incident
    feed, one entry per (node, url) at its latest occurrence in the window."""
    out: list[dict] = []
    for node, url, code, cnt, last_ts in _rows(
            conn, "SELECT node, url, http_code, COUNT(*), MAX(ts) FROM http_samples "
            "WHERE ts >= ? AND (http_code >= 500 OR http_code = 0) GROUP BY node, url", (since,)):
        if node is None or last_ts is None:
            continue
        out.append({"node": node, "klass": "http-error", "scope": query.host_label(url),
                    "detail": f"HTTP {code} x{cnt}", "severity": 3,
                    "start": last_ts, "end": last_ts, "duration_s": 0})
    return out


def ports(conn, node: str, now: float | None = None) -> dict:
    """Latest per-port connection snapshot for one node (from the ports probe): the most-recent
    sample batch, split into inbound listening services and outbound remote-service ports, each
    sorted busiest-first. Returns {now, node, ts, listen:[...], out:[...]}; empty when the node
    has no port_samples (probe not deployed / no data yet)."""
    now = time.time() if now is None else now
    row = _rows(conn, "SELECT MAX(ts) FROM port_samples WHERE node = ?", (node,))
    ts = row[0][0] if row else None
    if ts is None:
        return {"now": now, "node": node, "ts": None, "listen": [], "out": []}
    listen, out = [], []
    for proto, d, port, conns, peers, bsent, brecv in _rows(
            conn, "SELECT proto, dir, port, conns, peers, bytes_sent, bytes_recv FROM port_samples "
            "WHERE node = ? AND ts = ?", (node, ts)):
        rec = {"proto": proto, "port": port, "conns": conns or 0, "peers": peers or 0,
               "bytes_sent": bsent, "bytes_recv": brecv}
        (listen if d == "in" else out).append(rec)
    # busiest first: by bytes moved (sent+recv) then connection count
    def _busy(r):
        return (-((r["bytes_sent"] or 0) + (r["bytes_recv"] or 0)), -r["conns"], r["port"])
    listen.sort(key=_busy)
    out.sort(key=_busy)
    return {"now": now, "node": node, "ts": ts, "listen": listen, "out": out}


# Well-known port -> service label, so the network view reads as "https / redis / ssh" instead of
# bare numbers. The node probe records only the port; this is a hub-side display convenience.
_WELLKNOWN = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 67: "dhcp",
    68: "dhcp", 80: "http", 110: "pop3", 119: "nntp", 123: "ntp", 143: "imap", 161: "snmp",
    179: "bgp", 389: "ldap", 443: "https", 445: "smb", 465: "smtps", 514: "syslog", 587: "smtp",
    631: "ipp", 636: "ldaps", 873: "rsync", 993: "imaps", 995: "pop3s", 1194: "openvpn",
    1433: "mssql", 1521: "oracle", 1883: "mqtt", 2049: "nfs", 2375: "docker", 2376: "docker-tls",
    3000: "grafana", 3306: "mysql", 3389: "rdp", 4222: "nats", 5044: "logstash", 5432: "postgres",
    5601: "kibana", 5672: "amqp", 6379: "redis", 6443: "k8s-api", 8000: "http-alt",
    8080: "http-proxy", 8086: "influxdb", 8443: "https-alt", 8765: "smokemon", 9000: "http-alt",
    9090: "prometheus", 9092: "kafka", 9200: "elasticsearch", 11211: "memcached",
    15672: "rabbitmq", 25565: "minecraft", 27017: "mongodb", 51820: "wireguard"}


def app_label(port: int) -> str:
    """Friendly service name for a port, falling back to the bare number."""
    return _WELLKNOWN.get(port, f":{port}")


def network(conn, node=None, hours: float = 6.0, buckets: int = 60, now: float | None = None) -> dict:
    """Per-application throughput (bytes/s) over time from port_samples. Fleet-wide when node is
    None (each app summed across the fleet), or one node's ports when given. Throughput is the
    positive delta of the bucketed cumulative byte gauge / bucket-width (cumulative counters churn
    as connections come and go, so negative deltas - a closed connection - clamp to 0). Returns
    busiest-first apps each with a bytes/s series, ready for an area chart."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    width = max(1.0, (hours * 3600) / buckets)
    nf, npar = (" AND node=?", [node]) if node else ("", [])
    # bucketed cumulative gauge per (node, port, dir); delta'd into a rate below
    gauges: dict[tuple, dict[int, float]] = {}
    for nd, port, d, b, g in _rows(
            conn, "SELECT node, port, dir, CAST((ts-?)/? AS INT) b, "
            "AVG(COALESCE(bytes_sent,0)+COALESCE(bytes_recv,0)) FROM port_samples "
            "WHERE ts>=?" + nf + " GROUP BY node, port, dir, b", (since, width, since, *npar)):
        if port is None or b is None or not (0 <= b < buckets):
            continue
        gauges.setdefault((nd, port, d), {})[int(b)] = g or 0.0
    apps: dict[int, list] = {}  # port -> [bytes/s per bucket], summed across node+dir
    for (_nd, port, _d), gmap in gauges.items():
        arr = apps.setdefault(port, [0.0] * buckets)
        prev_b = prev_g = None
        for b in sorted(gmap):
            if prev_b is not None and b > prev_b:
                arr[b] += max(0.0, gmap[b] - prev_g) / width
            prev_b, prev_g = b, gmap[b]
    items = [{"port": p, "app": app_label(p), "series": [round(x, 1) for x in arr],
              "total": round(sum(arr), 1)} for p, arr in apps.items()]
    items.sort(key=lambda x: (-x["total"], x["port"]))
    top = 12 if node else 16
    return {"now": now, "hours": hours, "node": node or "", "buckets": buckets,
            "since": since, "width": width, "apps": items[:top]}


def ship_volume(conn, hours: float = 24.0, now: float | None = None) -> dict:
    """Measured ship cost per node: the ACTUAL compressed bytes each node pushed over the wire
    (summed from ingest_log, which records every POST's Content-Length), not a from-the-DB
    estimate. Answers 'is this node shipping a lot / wasteful data?'. Also returns the per-table
    row counts received in the window so you can see WHICH data dominates (e.g. a node shipping
    raw ping_rtts). Sorted heaviest-first. ingest_log only accrues from hub start, so a fresh
    hub shows little until traffic flows."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    rate = config.AWS_GB_COST  # $/GB applied to measured wire_bytes -> ingest cost per node
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
                     # ingest cost = measured GB x rate, for the window and projected per-day
                     "cost_window": round((wire or 0) / 1e9 * rate, 4),
                     "cost_per_day": round(per_day / 1e9 * rate, 4) if per_day else None,
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
    # smokemon's OWN compute footprint per node: collect-fast/slow + shipper each self-measure a
    # proc_samples row named 'smokemon'. Sum the latest sample per pid over a short recent window
    # (not the full `hours`, else a restarted collector's dead pid would double-count). cpu_pct is
    # per-core-% (can exceed 100 across processes); rss_mb is resident memory.
    recent = now - 300.0
    smoke: dict[str, dict] = {}
    for node, _pid, cpu, rss, _ts in _rows(
            conn, "SELECT node, pid, cpu_pct, rss_mb, MAX(ts) FROM proc_samples "
            "WHERE name = 'smokemon' AND ts >= ? GROUP BY node, pid", (recent,)):
        if node is None:
            continue
        s = smoke.setdefault(node, {"cpu": 0.0, "rss": 0.0})
        s["cpu"] += cpu or 0.0
        s["rss"] += rss or 0.0
    for node, s in smoke.items():
        d = agg.setdefault(node, {"node": node, "wire_bytes": 0, "raw_bytes": 0, "rows": 0,
                                  "posts": 0, "observed_s": 0, "wire_bytes_per_day": None,
                                  "rows_per_day": None, "cost_window": 0.0, "cost_per_day": None,
                                  "ratio": None, "top": []})
        d["smoke_cpu_pct"] = round(s["cpu"], 1)
        d["smoke_rss_mb"] = round(s["rss"], 1)
    out = sorted(agg.values(), key=lambda r: -(r["wire_bytes"] or 0))
    cost_day_total = round(sum(n["cost_per_day"] for n in out if n.get("cost_per_day")), 2)
    cost_window_total = round(sum(n.get("cost_window") or 0 for n in out), 2)
    return {"now": now, "hours": hours, "nodes": out, "gb_rate": rate,
            "cost_per_day_total": cost_day_total, "cost_window_total": cost_window_total}


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
    for (node, name, image, state, running, health, exit_code, restart_count,
         oom, cpu, mem, pids, ts) in _rows(
            conn, "SELECT node, name, image, state, running, health, exit_code, restart_count, "
            "oom_killed, cpu_pct, mem_mb, pids, MAX(ts) FROM docker_samples "
            "WHERE ts >= ? GROUP BY node, name", (since,)):
        if node is None:
            continue
        if name == "__daemon__":
            if not running:
                docker_down.append(node)
            continue
        v = {"node": node, "name": name, "image": image, "state": state, "running": running,
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


def inventory(conn, now: float | None = None) -> dict:
    """Per-node device/environment facts for the dashboard inventory view, from the delta-coded
    device_facts table: the latest value per (node, key) via the MAX(ts) bare-column trick (one
    GROUP BY query). Facts are grouped by kind (hw / os / net / runtime) so the UI can render a
    block per node, with each node's most recent fact change as its freshness. Empty when the
    inventory probe is unused fleet-wide."""
    now = time.time() if now is None else now
    by_node: dict[str, dict] = {}
    for node, key, value, kind, ts in _rows(
            conn, "SELECT node, key, value, kind, MAX(ts) FROM device_facts GROUP BY node, key"):
        if node is None or key is None:
            continue
        entry = by_node.setdefault(node, {"node": node, "facts": {}, "_last": None})
        entry["facts"][key] = {"value": value, "kind": kind or "runtime"}
        if ts is not None and (entry["_last"] is None or ts > entry["_last"]):
            entry["_last"] = ts
    out = sorted(by_node.values(), key=lambda r: r["node"])
    for e in out:
        last = e.pop("_last")
        e["updated_s"] = round(now - last) if last else None
    return {"now": now, "nodes": out}


def dashboard_html() -> str:
    """Self-contained fleet dashboard (no external assets). Polls /api/fleet-status and
    renders an ultra-dense, worst-first, colour-coded one-line-per-node grid. Refresh
    interval via ?refresh=SEC (default 5)."""
    return _DASHBOARD_HTML


# Favicon: the exact brand sparkline (same path as the header logo, stroke #58a6ff) on the
# dashboard's dark rounded tile, so the browser tab matches the header and /favicon.ico stops 404ing.
FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="32" height="32">'
    b'<rect width="24" height="24" rx="5" fill="#0b0e14"/>'
    b'<path d="M2 12h3.5l2-7 4 15 3-10 1.5 3H22" fill="none" stroke="#58a6ff" stroke-width="2" '
    b'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>smokemon fleet</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
 :root{
  --bg:#0b0e14;--bg2:#0e1118;--card:#11151d;--card2:#161b24;--line:#202632;--line2:#2c3340;
  --fg:#dfe5ec;--mut:#8b95a3;--dim:#5c6675;
  --ok:#3fb950;--okf:#54c266;--warn:#c98a16;--warnf:#d6a429;--down:#e5484d;--downf:#f0666b;
  --stale:#4d5663;--stalef:#8b95a3;--accent:#4493e0;
  --ok-bg:rgba(63,185,80,.12);--warn-bg:rgba(214,164,41,.12);--down-bg:rgba(229,72,77,.12);
  --stale-bg:rgba(120,131,148,.10);--accent-bg:rgba(68,147,224,.12);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --r:8px;--sh:none}
 *{box-sizing:border-box}
 ::selection{background:rgba(68,147,224,.3)}
 ::-webkit-scrollbar{width:10px;height:10px}
 ::-webkit-scrollbar-track{background:transparent}
 ::-webkit-scrollbar-thumb{background:var(--line2);border-radius:6px;border:2px solid var(--bg)}
 ::-webkit-scrollbar-thumb:hover{background:#39414f}
 body{margin:0;color:var(--fg);font:13px/1.4 var(--sans);background:var(--bg);
   -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 .num{font-family:var(--mono);font-variant-numeric:tabular-nums}
 /* ---- header ---- */
 header{position:sticky;top:0;z-index:30;background:rgba(11,14,20,.9);
   backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
 .hrow{display:flex;gap:14px;align-items:center;padding:7px 14px 0;flex-wrap:wrap}
 .hrow2{display:flex;gap:12px;align-items:center;padding:7px 14px;flex-wrap:wrap}
 .brand{display:flex;align-items:center;gap:8px;flex:0 0 auto}
 .brand svg{display:block}
 h1{font-size:13px;margin:0;font-weight:600;letter-spacing:.2px;color:var(--fg)}
 h1 b{color:var(--accent);font-weight:600;letter-spacing:1.2px;font-size:10px;padding:1px 6px;
   border:1px solid var(--line2);border-radius:4px;margin-left:4px;background:var(--accent-bg)}
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
 .s-healthy{background:var(--okf)}
 .s-warn{background:var(--warnf)}
 .s-down{background:var(--downf)}
 .s-stale{background:var(--stale)}
 #err{color:var(--downf);padding:7px 16px;font-family:var(--mono);font-size:12px;
   background:var(--down-bg);border-bottom:1px solid rgba(248,81,73,.25)}
 #err:empty{display:none}
 /* ---- generic primitives ---- */
 .card{background:var(--card);border:1px solid var(--line);border-radius:var(--r)}
 .card-h{display:flex;align-items:center;gap:8px;font-size:10.5px;text-transform:uppercase;
   letter-spacing:.7px;color:var(--mut);font-weight:600;padding:10px 13px 0}
 .card-h .card-sub{margin-left:auto;text-transform:none;letter-spacing:0;color:var(--dim);
   font-weight:400;font-family:var(--mono);font-size:11px}
 .view{padding:13px;animation:fade .18s ease}
 .view[hidden]{display:none}
 /* first-open warm-up: explain the one-time cache build instead of a grey blank */
 .loading{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:13px;padding:72px 20px;text-align:center}
 .loading .spin{width:30px;height:30px;border-radius:50%;border:3px solid var(--line2);
  border-top-color:var(--accent);animation:spin .8s linear infinite}
 .loading .lt{font-size:13px;color:var(--fg);font-weight:600}
 .loading .lh{font-size:12px;color:var(--dim);max-width:380px;line-height:1.55}
 @keyframes spin{to{transform:rotate(360deg)}}
 @media (prefers-reduced-motion:reduce){.loading .spin{animation-duration:2s}}
 @keyframes fade{from{opacity:0}to{opacity:1}}
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
 /* ---- overview (grid tab): compact summary strip + dense per-host card grid ---- */
 .ov{display:flex;flex-direction:column;gap:12px}
 .ov-strip{display:grid;grid-template-columns:auto auto 1fr;gap:10px;align-items:stretch}
 @media(max-width:900px){.ov-strip{grid-template-columns:1fr}}
 .donut-wrap{display:flex;align-items:center;gap:14px;padding:10px 14px 12px}
 .donut{width:104px;height:104px;flex:0 0 auto}
 .donut .track{fill:none;stroke:var(--card2);stroke-width:12}
 .donut .seg{fill:none;stroke-width:12;transform:rotate(-90deg);transform-origin:60px 60px;
   transition:stroke-dasharray .5s ease,stroke-dashoffset .5s ease}
 .donut .seg.healthy{stroke:var(--okf)}.donut .seg.warn{stroke:var(--warnf)}
 .donut .seg.down{stroke:var(--downf)}.donut .seg.stale{stroke:var(--stale)}
 .donut-total{font:700 26px/1 var(--mono);fill:var(--fg)}
 .donut-cap{font:600 8px var(--sans);fill:var(--dim);letter-spacing:1.4px;text-transform:uppercase}
 .donut-legend{display:flex;flex-direction:column;gap:5px;flex:1 1 auto}
 .lg{display:flex;align-items:center;gap:8px;font-size:12px}
 .lg .lg-label{color:var(--mut);flex:1 1 auto;text-transform:capitalize}
 .lg .lg-count{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:700;font-size:14px}
 .lg.healthy .lg-count{color:var(--okf)}.lg.warn .lg-count{color:var(--warnf)}
 .lg.down .lg-count{color:var(--downf)}.lg.stale .lg-count{color:var(--stalef)}
 .ingest-card{display:flex;flex-direction:column;min-width:240px}
 .gauge-row{display:flex;align-items:baseline;gap:7px;padding:6px 14px 0}
 .gval{font:700 30px/1 var(--mono);font-variant-numeric:tabular-nums;color:#8e96ff}
 .gunit{font-size:12px;color:var(--mut);font-weight:500}
 .igspark{width:100%;height:56px;display:block;margin-top:2px}
 .gsub{color:var(--dim);font-size:11px;font-family:var(--mono);padding:0 14px 12px}
 /* compact KPI rail along the right of the summary strip */
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:0;
   background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
 .kpi{padding:9px 12px;position:relative;border-left:1px solid var(--line)}
 .kpi:first-child{border-left:none}
 .kpi::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:transparent}
 .kpi.ok::after{background:var(--ok)}.kpi.warn::after{background:var(--warn)}.kpi.bad::after{background:var(--down)}
 .kpi .tv{font:700 20px/1.05 var(--mono);font-variant-numeric:tabular-nums;color:var(--fg)}
 .kpi.ok .tv{color:var(--okf)}.kpi.warn .tv{color:var(--warnf)}.kpi.bad .tv{color:var(--downf)}
 .kpi .tl{color:var(--mut);font-size:9.5px;text-transform:uppercase;letter-spacing:.6px;margin-top:5px}
 .kpi .meter{height:4px;border-radius:3px;background:var(--card2);margin-top:7px;overflow:hidden}
 .kpi .meter-fill{height:100%;border-radius:3px;width:0;transition:width .5s ease;background:var(--ok)}
 .kpi.warn .meter-fill{background:var(--warn)}.kpi.bad .meter-fill{background:var(--down)}
 /* per-host consolidated cards: one card = one host with net+sys+services rolled up */
 .hostgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:9px}
 .hcard{background:var(--card);border:1px solid var(--line);border-left:2px solid var(--stale);
   border-radius:var(--r);padding:9px 11px;cursor:pointer;transition:border-color .1s,background .1s}
 .hcard:hover{background:var(--card2);border-color:var(--line2)}
 .hcard.healthy{border-left-color:var(--ok)}.hcard.warn{border-left-color:var(--warn)}
 .hcard.down{border-left-color:var(--down)}.hcard.stale{border-left-color:var(--stale);opacity:.8}
 .hc-top{display:flex;align-items:center;gap:7px}
 .hc-name{font-weight:600;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
   flex:1 1 auto;font-family:var(--mono)}
 .hc-spark{width:54px;height:18px;flex:0 0 auto}
 .hc-chip{font:600 9px var(--mono);text-transform:uppercase;letter-spacing:.4px;padding:1px 5px;
   border-radius:3px;flex:0 0 auto}
 .hc-chip.healthy{background:var(--ok-bg);color:var(--okf)}.hc-chip.warn{background:var(--warn-bg);color:var(--warnf)}
 .hc-chip.down{background:var(--down-bg);color:var(--downf)}.hc-chip.stale{background:var(--stale-bg);color:var(--stalef)}
 /* metric strip: label over value, monospace, tight columns */
 .hc-metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:0 8px;margin-top:8px}
 .hc-m{display:flex;flex-direction:column;gap:1px;min-width:0}
 .hc-m .ml{font-size:8.5px;text-transform:uppercase;letter-spacing:.4px;color:var(--dim)}
 .hc-m .mv{font:600 12px var(--mono);font-variant-numeric:tabular-nums;color:var(--fg);
   overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .hc-m .mv.bad{color:var(--downf)}.hc-m .mv.warnv{color:var(--warnf)}.hc-m .mv.dim{color:var(--dim)}
 /* services badges row: docker/redis/procs/streams, only present when the host runs them */
 .hc-svc{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
 .hc-svc:empty{display:none}
 .svcb{display:inline-flex;align-items:center;gap:5px;padding:2px 7px;border-radius:4px;
   background:var(--card2);border:1px solid var(--line);font:600 10px var(--mono);color:var(--mut)}
 .svcb .sk{color:var(--dim);font-weight:500;text-transform:uppercase;letter-spacing:.3px;font-size:9px}
 .svcb .sv{color:var(--fg)}
 .svcb.ok{border-color:rgba(63,185,80,.3)}.svcb.ok .sv{color:var(--okf)}
 .svcb.warn{border-color:rgba(214,164,41,.35);background:var(--warn-bg)}.svcb.warn .sv{color:var(--warnf)}
 .svcb.bad{border-color:rgba(229,72,77,.4);background:var(--down-bg)}.svcb.bad .sv{color:var(--downf)}
 /* host-grid toolbar: density + sort + count */
 .hg-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .hg-bar .cnt{color:var(--dim);font-size:11px;font-family:var(--mono);margin-left:auto}
 .hostgrid.dense{grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:7px}
 .hostgrid.dense .hc-spark,.hostgrid.dense .hc-svc{display:none}
 .hostgrid.dense .hcard{padding:7px 9px}
 .hostgrid.dense .hc-metrics{grid-template-columns:repeat(3,1fr);gap:3px 8px;margin-top:6px}
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
 .tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(214px,1fr));gap:9px}
 .ntile{background:var(--card);border:1px solid var(--line);
   border-left:2px solid var(--stale);border-radius:var(--r);padding:11px 12px;cursor:pointer;
   transition:border-color .1s,background .1s}
 .ntile:hover{border-color:var(--line2);background:var(--card2)}
 .ntile.healthy{border-left-color:var(--ok)}.ntile.warn{border-left-color:var(--warn)}
 .ntile.down{border-left-color:var(--down)}.ntile.stale{border-left-color:var(--stale);opacity:.8}
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
 /* ---- risks: overview-style summary rail + per-node problem cards ---- */
 #risk{max-width:none}
 .risk-sum{margin-bottom:16px;grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}
 .pnodes{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:9px}
 .pnode{background:var(--card);border-radius:var(--r);padding:10px 12px;cursor:pointer;transition:background .1s}
 .pnode:hover{background:var(--card2)}
 .pnode-h{display:flex;align-items:center;gap:8px;margin-bottom:9px}
 .pdot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
 .pdot.sev3{background:var(--downf)}.pdot.sev2{background:var(--warnf)}.pdot.sev1{background:var(--accent)}
 .pnode-h .pn{font:600 12.5px var(--mono);flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .pnode-h .pc{font-size:10.5px;color:var(--dim);font-family:var(--mono);flex:0 0 auto}
 .pissues{display:flex;flex-direction:column;gap:5px}
 .pi{display:flex;align-items:center;gap:8px;font-size:11.5px;min-width:0}
 .pi-k{font:600 9px var(--mono);text-transform:uppercase;letter-spacing:.3px;padding:2px 6px;border-radius:4px;
   flex:0 0 auto;width:72px;text-align:center}
 .pi-d{color:var(--fg);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto}
 .pi-e{color:var(--dim);font:700 10.5px var(--mono);flex:0 0 auto}
 .pi-tag{font:600 9px var(--mono);padding:1px 6px;border-radius:4px;flex:0 0 auto;
   background:var(--accent-bg);color:var(--accent)}
 .pi-tag.muted{background:var(--card2);color:var(--dim)}
 .pi{cursor:default}
 .riskbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
 .kbtns{display:flex;gap:5px;flex-wrap:wrap}
 .kbtn{background:var(--card);border:1px solid var(--line);color:var(--mut);font:500 11px var(--mono);
   text-transform:uppercase;letter-spacing:.3px;padding:4px 9px;border-radius:7px;cursor:pointer;transition:.12s}
 .kbtn:hover{color:var(--fg);border-color:var(--line2)}
 .kbtn.on{color:var(--accent);border-color:var(--accent);background:var(--accent-bg)}
 .rd-row.rd-alert{flex-direction:column;align-items:stretch}
 .rd-row .rd-main{display:flex;align-items:center;gap:12px}
 .rd-kv{display:flex;flex-wrap:wrap;gap:6px 14px;margin:8px 0 2px 74px;font:500 11.5px var(--mono);color:var(--dim)}
 .rd-kv span b{color:var(--mut);font-weight:600}
 .rd-logs{cursor:pointer;color:var(--accent);font:600 11px var(--mono);margin-left:8px}
 .rd-logs:hover{text-decoration:underline}
 .pi.s3 .pi-k{background:var(--down-bg);color:var(--downf)}.pi.s3 .pi-e{color:var(--downf)}
 .pi.s2 .pi-k{background:var(--warn-bg);color:var(--warnf)}
 .pi.s1 .pi-k{background:var(--accent-bg);color:var(--accent)}
 .risk-legacy{max-width:1120px}
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
 .risk.disk,.risk.throttle,.risk.memory,.risk.sev3{border-left-color:var(--down)}
 .risk.disk .rk,.risk.throttle .rk,.risk.memory .rk,.risk.sev3 .rk,.risk.disk .ic,.risk.throttle .ic,.risk.memory .ic,.risk.sev3 .ic{color:var(--downf)}
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
 .ffill{height:100%;border-radius:5px;background:var(--accent);transition:width .5s ease}
 .fval{flex:0 0 auto;width:96px;text-align:right;font:600 12.5px var(--mono);font-variant-numeric:tabular-nums}
 .fcost{flex:0 0 auto;width:92px;text-align:right;color:var(--accent);font:600 12px var(--mono);font-variant-numeric:tabular-nums}
 .frpd{flex:0 0 auto;width:128px;text-align:right;color:var(--mut);font-size:11.5px;font-family:var(--mono)}
 .ftop{flex:0 0 auto;width:160px;color:var(--dim);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 @media(max-width:680px){.frpd,.ftop{display:none}}
 /* ---- detail modal ---- */
 #detail{position:fixed;inset:0;background:rgba(4,6,10,.74);backdrop-filter:blur(4px);
   -webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:18px;
   z-index:50;animation:fade .18s ease}
 #detail[hidden]{display:none}
 .dwin{background:var(--card);border:1px solid var(--line2);
   border-radius:10px;width:min(98vw,1700px);max-height:94vh;display:flex;flex-direction:column;
   overflow:hidden;box-shadow:0 16px 48px rgba(0,0,0,.5)}
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
 /* risks tab inside the detail modal: detailed per-node risk list */
 #drisk{margin:0;padding:12px 14px;background:var(--bg);overflow-y:auto;height:80vh}
 #drisk[hidden]{display:none}
 .rd-sec{font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.6px;color:var(--mut);
   margin:18px 2px 9px;display:flex;gap:8px;align-items:center}
 .rd-sec:first-child{margin-top:2px}
 .rd-sec span{color:var(--dim);font-weight:400}
 .rd-row{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:8px;background:var(--card);margin-bottom:6px}
 .rd-sev{flex:0 0 auto;width:62px;text-align:center;font:600 9px var(--mono);text-transform:uppercase;
   letter-spacing:.4px;padding:3px 0;border-radius:4px}
 .rd-row.s3 .rd-sev{background:var(--down-bg);color:var(--downf)}
 .rd-row.s2 .rd-sev{background:var(--warn-bg);color:var(--warnf)}
 .rd-row.s1 .rd-sev{background:var(--accent-bg);color:var(--accent)}
 .rd-kind{flex:0 0 auto;width:88px;font:600 11px var(--mono);text-transform:uppercase;letter-spacing:.3px;color:var(--fg)}
 .rd-detail{flex:1 1 auto;font-family:var(--mono);font-size:12.5px;color:var(--fg);min-width:0}
 .rd-tail{flex:0 0 auto;font:700 11.5px var(--mono);color:var(--dim)}
 .rd-row.s3 .rd-tail{color:var(--downf)}
 /* logs tab: filter bar (severity/kind/source) + sortable headers + clickable node + excerpt tails */
 .logbar{display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap}
 .logbar .fnote{margin:0 0 0 auto;color:var(--dim)}
 .logfilt{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
 .logfilt .lg-lbl{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.4px;color:var(--dim)}
 .lg-srch{background:var(--card);border:1px solid var(--line);color:var(--fg);font:500 12px var(--mono);
  padding:5px 10px;border-radius:7px;width:200px;outline:none}
 .lg-srch:focus{border-color:var(--accent)}
 .lg-head{display:flex;align-items:center;gap:12px;padding:6px 10px;border-bottom:1px solid var(--line2);
  font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.5px;color:var(--mut);user-select:none}
 .lg-head span[data-lsort]{cursor:pointer}.lg-head span[data-lsort]:hover{color:var(--fg)}
 .lg-head .ar{color:var(--accent);font-size:9px;margin-left:2px}
 .lg-h-sev{flex:0 0 auto;width:52px}.lg-h-node{flex:0 0 auto;width:120px}
 .lg-h-src{flex:0 0 auto;width:88px}.lg-h-det{flex:1 1 auto}.lg-h-when{flex:0 0 auto;width:64px;text-align:right}
 .lg-node{flex:0 0 auto;width:120px;font:600 11.5px var(--mono);color:var(--accent);cursor:pointer;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .lg-ex{margin:-2px 0 8px 12px;padding:8px 10px;background:var(--bg2);border:1px solid var(--line);
  border-radius:8px;font:12px/1.5 var(--mono);color:var(--mut);white-space:pre-wrap;word-break:break-word;
  max-height:220px;overflow:auto}
 /* network tab: per-application throughput cards (small multiples) */
 .netgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
 .netcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
 .netcard[data-node]{cursor:pointer}
 .netcard[data-node]:hover{border-color:var(--line2)}
 .nc-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
 .nc-app{font:600 13px var(--mono);color:var(--fg)}
 .nc-rate{font:600 11px var(--mono);color:var(--accent);flex:0 0 auto}
 .ntspark{width:100%;height:42px;display:block;margin:6px 0 4px}
 .nc-sub{font:11px var(--mono);color:var(--dim)}
 .rd-load{color:var(--dim);padding:14px;font-style:italic}
 /* ports tab inside the detail modal: two columns of per-port connection counts */
 #dports{margin:0;padding:12px 14px;background:var(--bg);overflow-y:auto;height:80vh}
 #dports[hidden]{display:none}
 .pt-cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
 @media(max-width:760px){.pt-cols{grid-template-columns:1fr}}
 .pt-tbl{border-collapse:separate;border-spacing:0;width:100%;font:12px var(--mono);font-variant-numeric:tabular-nums}
 .pt-tbl th{text-align:right;color:var(--mut);font-weight:600;text-transform:uppercase;font-size:9.5px;
   letter-spacing:.4px;padding:4px 10px}
 .pt-tbl th:first-child,.pt-tbl th:nth-child(2){text-align:left}
 .pt-tbl td{text-align:right;padding:4px 10px;color:var(--fg)}
 .pt-tbl td:first-child,.pt-tbl td.pt-port{text-align:left}
 .pt-port{color:var(--accent);font-weight:600}
 .pt-hot{color:var(--okf);font-weight:700}
 .pt-tbl tbody tr:hover{background:var(--card)}
 /* braille glyphs (plotext markers) come from a fallback font that is wider than the mono
    cell, which drifts every data row out of line with the ascii axes. --brls is measured at
    render time (mono cell minus braille cell, so negative) to pull each braille char back to
    exactly one cell -> the curve lines up again. */
 #dplot .br{letter-spacing:var(--brls,0px)}
 .dfoot{padding:9px 14px;border-top:1px solid var(--line);color:var(--dim);font-size:11.5px;font-family:var(--mono)}
 /* ===== flat look (user request): no frames, no coloured left accents, no shadows/strips.
    !important + a global box-shadow reset so nothing in the base sheet can win. ===== */
 .card,h1 b,.tab,.tab.on,input#q,.hb-track,.pill,.btn-grp,.kpis,.kpi,
 .hcard,.ntile,.risk,.svcb,.seg-ctl,.heat-legend .bar,.fbar,.svc-tbl,
 .dwin,.dh button,#dclose,header,#err,.dbar,.dfoot,.hc-svc,.dh.sep{border:none !important}
 .hcard,.ntile,.risk,.kpi{border-left:none !important}      /* coloured left strips */
 .kpi::after{display:none !important}                        /* coloured left bar (::after) */
 .grid-t th,.grid-t td,.svc-tbl th,.svc-tbl td{border-bottom:none !important}  /* line-less tables */
 *{box-shadow:none !important}   /* drop ALL shadows incl. active tab/button accent strips + modal */
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
<div id="net" class="view" hidden></div>
<div id="risk" class="view" hidden></div>
<div id="logs" class="view" hidden></div>
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
  <div id="drisk" hidden></div>
  <div id="dports" hidden></div>
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
let gotData=false;  // has the first /api/fleet-status arrived? grid/table show the warm-up until it has
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
 // grid/table paint from the live poll, not a server cache, but on the very first boot last is
 // still empty until /api/fleet-status returns - show the warm-up instead of an empty shell.
 if(!gotData&&(view==="grid"||view==="table")){viewEl(view).innerHTML=loadingHtml(view);return;}
 if(view==="grid")renderGrid();
 else if(view==="table"){
  const term=q.value.trim().toLowerCase();
  renderTable((last.nodes||[]).filter(n=>!term||n.node.toLowerCase().includes(term)));
 }
}
// ---- fleet overview (grid tab): status donut + ingest area gauge + KPI cards ------------
// static skeleton (no dynamic data -> safe innerHTML once); all live values are written via
// textContent / setAttribute below so dynamic strings are never interpolated into markup.
const GRID_SKELETON=`<div class="ov"><div class="ov-strip">`
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
 +`<div class="card ingest-card"><div class="card-h">hub ingest<span class="card-sub">realtime</span></div>`
 +`<div class="gauge-row"><span class="gval" id="ig-rate">--</span><span class="gunit">KB/s</span></div>`
 +`<svg class="igspark" id="ig-spark" viewBox="0 0 100 36" preserveAspectRatio="none">`
 +`<path id="ig-area" fill="url(#gIngest)"/>`
 +`<polyline id="ig-poly" fill="none" stroke="#7c83ff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
 +`<circle id="ig-dot" r="0" fill="#9ea4ff"/></svg>`
 +`<span class="gsub" id="ig-sub">waiting for ingest…</span></div>`
 +`<div class="kpis" id="agg-tiles"></div></div>`
 +`<div class="hg-bar"><div class="seg-ctl" id="hg-dens"><button data-d="full" class="on">cards</button><button data-d="dense">dense</button></div>`
 +`<div class="seg-ctl" id="hg-sort"><button data-s="state" class="on">by health</button><button data-s="node">by name</button><button data-s="cpu">by cpu</button><button data-s="rtt">by rtt</button></div>`
 +`<span class="cnt" id="hg-cnt"></span></div>`
 +`<div class="hostgrid" id="hostgrid"></div></div>`;
const GRID_TILES=[["nodes","nodes"],["rtt","avg rtt"],["loss","avg loss"],["cpu","avg cpu"],
 ["mem","avg mem"],["temp","max temp"],["ship","ship / day"],["avgfoot","avg footprint"],["rows","rows / day"],
 ["smokecpu","smoke cpu"]];
const DONUT_C=2*Math.PI*52;  // donut ring circumference (r=52)
let gridBuilt=false,igRate,igSub,igArea,igPoly,igDot,tileVals={},meterFills={},donutSegs={},donutTotal,legendCounts={},hostgrid;
// host-grid view options: density (full cards vs dense) + sort key. persisted in the closure.
let hgDensity="full",hgSort="state";
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
 hostgrid=document.getElementById("hostgrid");
 // toolbar: density + sort segmented controls re-render the host cards in place.
 grid.querySelector("#hg-dens").addEventListener("click",e=>{const b=e.target.closest("[data-d]");
  if(!b)return;hgDensity=b.dataset.d;grid.querySelectorAll("#hg-dens button").forEach(x=>x.classList.toggle("on",x===b));renderHosts();});
 grid.querySelector("#hg-sort").addEventListener("click",e=>{const b=e.target.closest("[data-s]");
  if(!b)return;hgSort=b.dataset.s;grid.querySelectorAll("#hg-sort button").forEach(x=>x.classList.toggle("on",x===b));renderHosts();});
 nodeClick(hostgrid);
 gridBuilt=true;
}
function kpiTone(v,warn,bad){return v==null?"":v>=bad?"bad":v>=warn?"warn":"ok";}
function setKpi(k,text,tone){const v=tileVals[k];v.textContent=text;
 const card=v.parentElement;card.classList.remove("ok","warn","bad");if(tone)card.classList.add(tone);}
function renderGrid(){
 if(!gotData)return;  // wait for the first poll; render() is showing the warm-up
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
  tileVals.avgfoot.textContent=fv.length?fmtKB(sd/fv.length)+"/d":"--";  // avg measured footprint per node
  const sv=fv.filter(x=>x.smoke_cpu_pct!=null);  // smokemon's own avg cpu across reporting nodes
  tileVals.smokecpu.textContent=sv.length?(sv.reduce((s,x)=>s+x.smoke_cpu_pct,0)/sv.length).toFixed(1)+"%":"--";
 }else{tileVals.ship.textContent="--";tileVals.rows.textContent="--";tileVals.avgfoot.textContent="--";tileVals.smokecpu.textContent="--";}
 renderHosts();
 renderIngest();
}
// ---- per-host consolidated cards: one card rolls up a host's network + system + every
// service it runs (docker / redis / watched procs / stream probes). joins fleet-status with
// the /api/services cache (svc) + the spark + cost caches. all node-controlled strings go
// through esc() before innerHTML. ----------------------------------------------------------
function hostCard(n){
 const s=svc[n.node]||{},rc=n.rtt_ms==null?"":n.rtt_ms>250?"bad":n.rtt_ms>120?"warnv":"";
 const lc=n.loss_pct!=null&&n.loss_pct>0?"bad":"",tc=n.temp==null?"":n.temp>=80?"bad":n.temp>=70?"warnv":"";
 const cc=n.cpu==null?"":n.cpu>=90?"bad":n.cpu>=70?"warnv":"",mc=n.mem==null?"":n.mem>=90?"bad":n.mem>=75?"warnv":"";
 const m=(l,v,cls)=>`<div class="hc-m"><span class="ml">${l}</span><span class="mv ${cls||(v==="--"?"dim":"")}">${v}</span></div>`;
 const metrics=m("rtt",fmtRtt(n.rtt_ms),rc)+m("loss",n.loss_pct==null?"--":Math.round(n.loss_pct)+"%",lc)
  +m("cpu",n.cpu==null?"--":Math.round(n.cpu)+"%",cc)+m("mem",n.mem==null?"--":Math.round(n.mem)+"%",mc)
  +m("temp",n.temp==null?"--":Math.round(n.temp)+"°",tc)+m("seen",fmtAge(n.age_s));
 // services row: a badge per service kind, coloured by worst member. only what the host runs.
 let svcb="";
 const dk=s.docker||[];
 if(dk.length||(s.docker_down)){const bad=dk.filter(c=>c.bad).length,up=dk.filter(c=>c.running).length;
  const cls=s.docker_down?"bad":bad?"bad":up<dk.length?"warn":"ok";
  svcb+=`<span class="svcb ${cls}"><span class="sk">dkr</span><span class="sv">${s.docker_down?"daemon down":up+"/"+dk.length+(bad?" "+bad+"!":"")}</span></span>`;}
 (s.redis||[]).forEach(r=>{const up=(r.connected||0)>=1;const cls=!up?"bad":(r.blocked_clients?"warn":"ok");
  svcb+=`<span class="svcb ${cls}"><span class="sk">redis</span><span class="sv">${up?(r.used_memory_mb!=null?Math.round(r.used_memory_mb)+"M":"up")+(r.ops_per_sec!=null?" "+Math.round(r.ops_per_sec)+"o/s":""):"down"}</span></span>`;});
 (s.procs||[]).forEach(p=>{const up=(p.count||0)>0;const cls=up?(p.restarts?"warn":"ok"):"bad";
  svcb+=`<span class="svcb ${cls}"><span class="sk">${esc(p.label)}</span><span class="sv">${up?"x"+p.count:"down"}${p.cpu_pct!=null&&up?" "+Math.round(p.cpu_pct)+"%":""}</span></span>`;});
 const st=s.streams||[];
 if(st.length){const ok=st.filter(x=>x.ok).length;const cls=ok<st.length?"bad":"ok";
  svcb+=`<span class="svcb ${cls}"><span class="sk">strm</span><span class="sv">${ok}/${st.length}</span></span>`;}
 return `<div class="hcard ${n.state}" data-node="${esc(n.node)}">`
  +`<div class="hc-top"><span class="st s-${n.state}"></span>`
  +`<span class="hc-name">${esc(n.node)}</span>`
  +(sparkArea(n.node,"hc-spark")||"")
  +`<span class="hc-chip ${n.state}">${esc(n.state)}</span></div>`
  +`<div class="hc-metrics">${metrics}</div>`
  +`<div class="hc-svc">${svcb}</div></div>`;
}
function hostSortVal(n,k){
 if(k==="node")return n.node.toLowerCase();
 if(k==="state")return ({down:0,warn:1,stale:2,healthy:3})[n.state];
 if(k==="cpu")return n.cpu==null?-1:n.cpu;
 if(k==="rtt")return n.rtt_ms==null?-1:n.rtt_ms;
 return 0;
}
function renderHosts(){
 if(!gridBuilt||!hostgrid)return;
 const term=q.value.trim().toLowerCase();
 const ns=(last.nodes||[]).filter(n=>!term||n.node.toLowerCase().includes(term));
 // state + node sort ascending (state already encodes worst-first as low values);
 // cpu + rtt sort descending (hottest / slowest first). node breaks every tie.
 const dir=(hgSort==="cpu"||hgSort==="rtt")?-1:1;
 ns.sort((a,b)=>{const va=hostSortVal(a,hgSort),vb=hostSortVal(b,hgSort);
  if(va!==vb)return (va<vb?-1:1)*dir;
  return a.node.toLowerCase()<b.node.toLowerCase()?-1:1;});
 hostgrid.className="hostgrid"+(hgDensity==="dense"?" dense":"");
 hostgrid.innerHTML=ns.length?ns.map(hostCard).join(""):`<div class="empty">no nodes reporting yet</div>`;
 const cnt=document.getElementById("hg-cnt");if(cnt)cnt.textContent=ns.length+" host"+(ns.length===1?"":"s");
}
// services cache (/api/services) rolled up per node for the host cards. shared, cached ~20s.
let svc={},svcTs=0;
async function loadSvc(force){
 if(!force&&Object.keys(svc).length&&Date.now()-svcTs<20000)return;
 try{const r=await fetch("/api/services",{cache:"no-store"});if(!r.ok)return;
  const d=await r.json(),by={};
  const ensure=n=>by[n]||(by[n]={docker:[],redis:[],procs:[],streams:[],docker_down:false});
  (d.docker||[]).forEach(c=>ensure(c.node).docker.push(c));
  (d.docker_down||[]).forEach(n=>ensure(n).docker_down=true);
  (d.redis||[]).forEach(r=>ensure(r.node).redis.push(r));
  (d.procs||[]).forEach(p=>ensure(p.node).procs.push(p));
  (d.streams||[]).forEach(s=>ensure(s.node).streams.push(s));
  svc=by;svcTs=Date.now();
 }catch(e){}
}
// live ingest gauge: current KB/s + a 15-min wire-bytes area chart from /api/ingest-rate.
let ingest=null;
function renderIngest(){
 if(view!=="grid"||!gotData)return;  // don't build the skeleton over the warm-up
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
 if(k==="smoke"){return (foot[n.node]||{}).smoke_cpu_pct||0;}
 return n[k];
}
function meterCell(v,warn,bad){if(v==null)return "--";
 const t=v>=bad?"bad":v>=warn?"warn":"",w=Math.max(0,Math.min(100,v));
 return `<span class="mini ${t}"><span class="mbar"><i style="width:${w.toFixed(0)}%"></i></span><b>${Math.round(v)}%</b></span>`;}
function tableHtml(nodes){
 const tcls=(v,warn,bad)=>v==null?"":v>=bad?"bad":v>=warn?"warnv":"";
 const cols=[["state",""],["node","node"],["rtt_ms","rtt"],["loss_pct","loss"],["cpu","cpu"],["mem","mem"],
  ["temp","temp"],["_trend","trend"],["age_s","seen"],["rows","rows/d"],["ship","ship/d"],["smoke","smoke"]];
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
   +`<td>${sd==null?"--":fmtKB(sd)+(f.wire_bytes_per_day!=null?"/d":"")}</td>`
   +`<td title="smokemon's own cpu/mem">${f.smoke_cpu_pct==null?"--":Math.round(f.smoke_cpu_pct)+"% · "+(f.smoke_rss_mb==null?"?":Math.round(f.smoke_rss_mb)+"MB")}</td></tr>`;
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
  last=await r.json();gotData=true;err.textContent="";
  meta.textContent=`${last.nodes.length} nodes · updated ${new Date().toLocaleTimeString()} · refresh ${REFRESH/1000}s`;
  render();
 }catch(e){err.textContent="fetch error: "+e.message;}
}
q.addEventListener("input",render);

// ---- view tabs: grid (live) · ranking · heatmap · risks. Only the active non-grid view
// polls (slow, 15s); grid status + sparklines + header pills always refresh. -------------
const VIEWS=[["grid","grid"],["table","table"],["rank","ranking"],["heat","heatmap"],["net","network"],["risk","risks"],["logs","logs"],["svc","services"],["cost","cost"]];
let view="grid",heatMetric="loss",heatHours=24;
// measured ship-cost cache (/api/cost): actual compressed bytes each node pushed over the wire,
// per node. shared by cost view, ranking columns and the modal stat line. cached ~25s.
let foot={},footTs=0,costRate=0,costDayTotal=0;
function fmtKB(b){if(b==null)return"?";const u=["B","KB","MB","GB","TB"];let v=b,i=0;while(v>=1024&&i<u.length-1){v/=1024;i++;}return(i?v.toFixed(1):v.toFixed(0))+" "+u[i];}
function fmtK(n){return n==null?"--":n>=1000?(n/1000).toFixed(0)+"k":""+n;}
async function loadFoot(force){
 if(!force&&Object.keys(foot).length&&Date.now()-footTs<25000)return;
 try{const r=await fetch("/api/cost?hours=24",{cache:"no-store"});if(!r.ok)return;
  const d=await r.json();foot={};(d.nodes||[]).forEach(x=>foot[x.node]=x);
  costRate=d.gb_rate||0;costDayTotal=d.cost_per_day_total||0;footTs=Date.now();}catch(e){}
}
const tabs=document.getElementById("tabs"),viewEl=id=>document.getElementById(id);
tabs.innerHTML=VIEWS.map(([id,l])=>`<div class="tab" data-v="${id}">${l}</div>`).join("");
// these tabs are painted wholesale by an /api/* fetch that hits a server-side cache; the first
// open is a cache miss (reads history) so it lags. Show why, instead of a grey blank, until the
// loader overwrites innerHTML. Re-opens already hold content, so no spinner flash on return.
const WARMUP={grid:"the fleet overview",table:"the per-node table",
 rank:"the 24-hour ranking",heat:"the latency heatmap",risk:"the risk death-clocks + incident feed",
 logs:"the fleet event & log stream",net:"the per-application network view",
 cost:"the measured ship-cost view",svc:"the fleet service telemetry"};
function loadingHtml(v){return `<div class="loading"><div class="spin"></div>`
 +`<div class="lt">building ${WARMUP[v]}…</div>`
 +`<div class="lh">first open reads each node's history and warms the hub's cache — this view stays instant on every later visit.</div></div>`;}
function setView(v){view=v;
 VIEWS.forEach(([id])=>viewEl(id).hidden=(id!==v));
 tabs.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.v===v));
 q.style.display=(v==="table"||v==="grid"||v==="logs"||v==="net"||v==="risk")?"":"none";  // node filter (net: drill-in)
 const el=viewEl(v);
 if(WARMUP[v]&&!el.firstChild)el.innerHTML=loadingHtml(v);  // first visit only (empty view)
 refreshView();}
function refreshView(){if(view==="rank")loadRank();else if(view==="heat")loadHeat();else if(view==="risk")loadRisk();else if(view==="logs")loadLogs();else if(view==="net")loadNet();else if(view==="cost")loadCost();else if(view==="svc")loadServices();
 else if(view==="table"){render();loadFoot().then(render);}
 else if(view==="grid"){render();ingestTick();sparkTick();
  loadFoot().then(()=>{if(view==="grid")renderGrid();});
  loadSvc().then(()=>{if(view==="grid")renderHosts();});}}
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
 const usd=v=>v==null?"--":"$"+(v>=1?v.toFixed(2):v.toFixed(4));
 const bars=ns.map(x=>{const v=val(x),pd=x.wire_bytes_per_day!=null;
  const top=x.top&&x.top.length?x.top.map(t=>t.t.replace("_samples","")).join(", "):"";
  const c=pd?x.cost_per_day:x.cost_window;
  return `<div class="frow" data-node="${esc(x.node)}" title="${x.posts} posts · observed ${fmtDur(x.observed_s)} · gzip ${x.ratio?x.ratio+":1":"?"} · raw ${fmtKB(x.raw_bytes)}"><span class="fname">${esc(x.node)}</span><div class="fbar"><div class="ffill" style="width:${(100*v/max).toFixed(1)}%"></div></div><span class="fval">${fmtKB(v)}${pd?"/day":""}</span><span class="fcost" title="ingest cost @ $${costRate}/GB">${usd(c)}${pd?"/day":""}</span><span class="frpd">${fmtK(x.rows_per_day!=null?x.rows_per_day:x.rows)} rows${pd?"/day":""}</span><span class="ftop">${esc(top)}</span></div>`;}).join("");
 const tot=costDayTotal?`fleet ingest cost ≈ <b>$${costDayTotal.toFixed(2)}/day</b> @ $${costRate}/GB · `:"";
 viewEl("cost").innerHTML=`<div class="fnote">${tot}actual compressed bytes shipped to the hub per node (gzip on the wire, measured from POST sizes, ~24h). top tables show where the volume goes. <span style="color:var(--dim)">AWS data-in is free — set SMOKEMON_AWS_GB_COST to your real rate (0 / NAT 0.045 / egress 0.09).</span></div>${bars}`;
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
 throttle:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13V5a2 2 0 0 1 4 0v8a4 4 0 1 1-4 0z"/></svg>`,
 memory:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="7" width="18" height="11" rx="1"/><path d="M7 7V4M12 7V4M17 7V4M7 18v2M12 18v2M17 18v2"/></svg>`,
 docker:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="10" width="4" height="4"/><rect x="8" y="10" width="4" height="4"/><rect x="13" y="10" width="4" height="4"/><path d="M3 18h16a4 4 0 0 0 4-4"/></svg>`,
 redis:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>`,
 proc:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9l3 3-3 3M13 15h3"/></svg>`,
 stream:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3 7 4-14 3 7h4"/></svg>`,
 tcp:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7.7 7.7l3 8M16.3 7.7l-3 8"/></svg>`,
 anomaly:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>`};
function riskIcon(k){return RISK_ICON[k]||RISK_ICON.disk;}
async function loadRisk(){try{const r=await fetch("/api/risks?hours=24",{cache:"no-store"});if(!r.ok)return;
 renderRisk(await r.json());}catch(e){}}
// Overview-style risks: every clock/alert/incident is folded into ONE issue list per node, so
// the page is a scannable grid of "problem nodes" (worst-first) + a summary rail — same mental
// model as the grid tab. Each issue: {sev 1-3, kind, detail, eta?|ago?}. Click a card -> modal.
// Risk view keeps its last payload so the filter bar (severity / kind / hide-muted / #q host)
// re-renders instantly without refetching (/api/risks is cached client-side anyway).
let riskData=null;const riskFilter={sev:"all",kinds:new Set(),hideMuted:false};
const RISK_SEVS=[["all","all"],["crit","critical"],["warn","warn+"]];
function renderRisk(d){riskData=d;drawRisk();}
function drawRisk(){
 const d=riskData||{};
 const clocks=d.clocks||[],alerts=d.alerts||[],incidents=d.incidents||[],anomalies=d.anomalies||[];
 // fold every clock/alert/incident into one issue list. text = short chip label; full = the
 // headline shown in the chip's hover tooltip; alerts carry their mute/firing delivery state.
 const issues=[];
 const add=(node,sev,kind,cat,text,full,x)=>issues.push({node,sev,kind,cat,text,full,...(x||{})});
 clocks.forEach(c=>add(c.node,c.severity||1,c.kind,"clock",c.detail,c.detail,{eta:c.eta_s}));
 alerts.forEach(a=>add(a.node,a.severity,a.kind,"alert",a.summary||a.detail,
  (a.label?a.label+" · ":"")+a.detail,{muted:a.muted,notified:a.notified,sinceS:a.since_s}));
 incidents.forEach(i=>add(i.node,i.severity,i.klass,"incident",i.scope+" · "+i.detail,
  i.scope+" · "+i.detail,{ago:i.start}));
 anomalies.forEach(a=>add(a.node,a.severity||1,"anomaly","anomaly","co-deviation ·"+a.score,
  a.detail,{ago:a.ts}));
 const allKinds=[...new Set(issues.map(x=>x.kind))].sort();
 const f=riskFilter,hostq=(view==="risk"?q.value.trim().toLowerCase():"");
 const pass=x=>{
  if(f.sev==="crit"&&x.sev!==3)return false;
  if(f.sev==="warn"&&x.sev<2)return false;
  if(f.kinds.size&&!f.kinds.has(x.kind))return false;
  if(f.hideMuted&&x.muted)return false;
  if(hostq&&!x.node.toLowerCase().includes(hostq))return false;
  return true;};
 const vis=issues.filter(pass),byNode={};
 vis.forEach(x=>{(byNode[x.node]=byNode[x.node]||[]).push(x);});
 const nodes=Object.keys(byNode).map(n=>{const list=byNode[n].sort((a,b)=>b.sev-a.sev);
  return {node:n,list,worst:Math.max(...list.map(x=>x.sev)),cnt:list.length};})
  .sort((a,b)=>b.worst-a.worst||b.cnt-a.cnt||a.node.localeCompare(b.node));
 const crit=vis.filter(x=>x.sev===3).length,warn=vis.filter(x=>x.sev===2).length,watch=vis.filter(x=>x.sev===1).length;
 const soon=clocks.filter(c=>c.eta_s!=null).sort((a,b)=>a.eta_s-b.eta_s)[0];
 const tile=(cls,val,label)=>`<div class="kpi ${cls}"><div class="tv">${val}</div><div class="tl">${label}</div></div>`;
 const strip=`<div class="kpis risk-sum">`
  +tile(crit?"bad":"",crit,"critical")+tile(warn?"warn":"",warn,"warnings")
  +tile("",watch,"watch")+tile(crit||warn?"warn":"",nodes.length,"nodes affected")
  +tile("",soon?fmtDur(soon.eta_s):"—",soon?"soonest · "+soon.kind:"death clock")+`</div>`;
 const seg=`<div class="seg-ctl" id="rsev">`+RISK_SEVS.map(([k,l])=>`<button data-rsev="${k}" class="${f.sev===k?"on":""}">${l}</button>`).join("")+`</div>`;
 const kinds=allKinds.map(k=>`<button class="kbtn ${f.kinds.has(k)?"on":""}" data-rkind="${esc(k)}">${esc(k)}</button>`).join("");
 const mute=`<button class="kbtn ${f.hideMuted?"on":""}" data-rmute="1">hide muted</button>`;
 const bar=`<div class="riskbar">${seg}<div class="kbtns">${kinds}${mute}</div></div>`;
 const cards=nodes.length?nodes.map(N=>{
  const chips=N.list.map(x=>{const tail=x.eta!=null?`<span class="pi-e">${esc(fmtDur(x.eta))}</span>`
    :(x.ago?`<span class="pi-e">${esc(tago(x.ago))}</span>`:"");
   const tags=(x.sinceS!=null?`<span class="pi-tag">firing ${esc(fmtDur(x.sinceS))}</span>`:"")
    +(x.muted?`<span class="pi-tag muted">muted</span>`:x.notified?`<span class="pi-tag">paged</span>`:"");
   return `<div class="pi s${x.sev}" title="${esc(x.full)}"><span class="pi-k">${esc(x.kind)}</span><span class="pi-d">${esc(x.text)}</span>${tags}${tail}</div>`;}).join("");
  return `<div class="pnode" data-node="${esc(N.node)}"><div class="pnode-h"><span class="pdot sev${N.worst}"></span>`
   +`<span class="pn">${esc(N.node)}</span><span class="pc">${N.cnt} issue${N.cnt>1?"s":""}</span></div>`
   +`<div class="pissues">${chips}</div></div>`;}).join(""):`<div class="empty">${issues.length?"no issues match the filter":"no problems detected — fleet healthy"}</div>`;
 viewEl("risk").innerHTML=strip+bar+`<div class="pnodes">${cards}</div>`;
}
// logs tab (/api/logs): fleet-wide newest-first stream of ext_events + log_excerpts. We fetch the
// full window (severity=all) once, keep the payload, and do severity / kind / source / text
// filtering + column sorting entirely client-side so the filter bar re-renders instantly.
let logData=null;
const logFilter={sev:"all",kinds:new Set(),sources:new Set(),text:"",sort:{key:"ts",dir:-1}};
const LOG_SEVS=[["all","all"],["error","error"],["warn","warn"],["info","info"]];
const logSevName=s=>s===3?"error":s===2?"warn":"info";
async function loadLogs(){
 const node=q.value.trim();
 const qs="severity=all&hours=24"+(node?"&node="+encodeURIComponent(node):"");
 try{const r=await fetch("/api/logs?"+qs,{cache:"no-store"});if(!r.ok)return;
  renderLogs(await r.json());}catch(e){}}
function renderLogs(d){logData=d;drawLogs();}
function drawLogs(){
 const d=logData||{},rows=d.rows||[],f=logFilter;
 // a log-excerpt row is its own "log" kind; events keep their numeric sev for the kind chip.
 const kindOf=r=>r.kind==="log"?"log":logSevName(r.sev);
 const allKinds=[...new Set(rows.map(kindOf))].sort();
 const allSources=[...new Set(rows.map(r=>r.source||"--"))].sort();
 const txt=f.text.trim().toLowerCase();
 const pass=r=>{
  if(f.sev==="error"&&r.sev!==3)return false;
  if(f.sev==="warn"&&r.sev!==2)return false;
  if(f.sev==="info"&&r.sev!==1)return false;
  if(f.kinds.size&&!f.kinds.has(kindOf(r)))return false;
  if(f.sources.size&&!f.sources.has(r.source||"--"))return false;
  if(txt){const hay=((r.label||"")+" "+(r.detail||"")+" "+(r.source||"")+" "+(r.node||"")).toLowerCase();
   if(!hay.includes(txt))return false;}
  return true;};
 const vis=rows.filter(pass);
 const sk=f.sort.key,dir=f.sort.dir;
 const sval=r=>sk==="sev"?r.sev:sk==="node"?(r.node||""):sk==="source"?(r.source||""):r.ts;
 vis.sort((a,b)=>{const x=sval(a),y=sval(b);
  return (x<y?-1:x>y?1:0)*dir || (b.ts-a.ts);});  // stable tiebreak: newest first
 const seg=`<div class="seg-ctl" id="logsev">`
  +LOG_SEVS.map(([k,l])=>`<button data-lsev="${k}" class="${f.sev===k?"on":""}">${l}</button>`).join("")+`</div>`;
 const kinds=allKinds.map(k=>`<button class="kbtn ${f.kinds.has(k)?"on":""}" data-lkind="${esc(k)}">${esc(k)}</button>`).join("");
 const srcs=allSources.map(s=>`<button class="kbtn ${f.sources.has(s)?"on":""}" data-lsrc="${esc(s)}">${esc(s)}</button>`).join("");
 const srch=`<input id="logq" class="lg-srch" type="text" placeholder="search text…" value="${esc(f.text)}">`;
 const bar=`<div class="logfilt">${seg}`
  +(allKinds.length>1?`<span class="lg-lbl">kind</span><div class="kbtns">${kinds}</div>`:"")
  +(allSources.length>1?`<span class="lg-lbl">source</span><div class="kbtns">${srcs}</div>`:"")
  +`${srch}</div>`;
 const arrow=k=>f.sort.key===k?`<span class="ar">${dir<0?"▼":"▲"}</span>`:"";
 const head=`<div class="lg-head">`
  +`<span class="lg-h-sev" data-lsort="sev">sev${arrow("sev")}</span>`
  +`<span class="lg-h-node" data-lsort="node">node${arrow("node")}</span>`
  +`<span class="lg-h-src" data-lsort="source">source${arrow("source")}</span>`
  +`<span class="lg-h-det">detail</span>`
  +`<span class="lg-h-when" data-lsort="ts">time${arrow("ts")}</span></div>`;
 const note=`<div class="logbar"><span class="fnote">${vis.length} of ${rows.length} `
  +`· errors are expedited to the hub on capture</span></div>`;
 const body=vis.length?vis.map(r=>{
  const when=`<span class="rd-tail">${esc(tago(r.ts))}</span>`;
  const node=`<span class="lg-node" data-node="${esc(r.node)}">${esc(r.node)}</span>`;
  if(r.kind==="log"){
   const drop=r.dropped?` · +${fmtKB(r.dropped)} dropped`:"",trunc=r.truncated?" · truncated":"";
   return `<div class="rd-row s2"><span class="rd-sev">log</span>${node}`
    +`<span class="rd-detail">${esc(r.source)} · ${esc(r.label)}${drop}${trunc}</span>${when}</div>`
    +`<pre class="lg-ex">${esc(r.detail)}</pre>`;}
  return `<div class="rd-row s${r.sev}"><span class="rd-sev">${logSevName(r.sev)}</span>${node}`
   +`<span class="rd-kind">${esc(r.source)}</span>`
   +`<span class="rd-detail">${esc(r.label)}${r.detail?" · "+esc(r.detail):""}</span>${when}</div>`;
 }).join(""):`<div class="empty">${rows.length?"no rows match the filter":"no events in this window"}</div>`;
 const el=viewEl("logs"),hadFocus=document.activeElement&&document.activeElement.id==="logq";
 const caret=hadFocus?document.activeElement.selectionStart:null;
 el.innerHTML=bar+note+head+body;
 if(hadFocus){const s=el.querySelector("#logq");if(s){s.focus();
  if(caret!=null)try{s.setSelectionRange(caret,caret);}catch(e){}}}
}
// network tab (/api/network): per-application throughput (bytes/s) over ~6h. No node filter ->
// fleet-wide (each app summed across nodes); type a node in #q to drill into that node's ports.
function netSpark(s){
 const ys=s.map(v=>v||0),hi=Math.max(...ys)||1,xmax=Math.max(1,s.length-1);
 const X=i=>(i/xmax*100).toFixed(1),Y=v=>(38-(v/hi)*34).toFixed(1);
 const pts=ys.map((v,i)=>X(i)+" "+Y(v));
 return `<svg class="ntspark" viewBox="0 0 100 40" preserveAspectRatio="none">`
  +`<path d="M0,40 L${pts.join(" L")} L100,40 Z" fill="url(#gIngest)"/>`
  +`<path d="M${pts.join(" L")}" fill="none" stroke="#7c83ff" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}
async function loadNet(){
 const node=q.value.trim();
 try{const r=await fetch("/api/network?hours=6"+(node?"&node="+encodeURIComponent(node):""),{cache:"no-store"});
  if(!r.ok)return;renderNet(await r.json());}catch(e){}}
function renderNet(d){
 const apps=d.apps||[],node=d.node;
 const hdr=`<div class="logbar"><span class="fnote">throughput per application · `
  +`${node?("node <b>"+esc(node)+"</b>"):"<b>fleet-wide</b>"} · last ${d.hours}h`
  +`${node?"":" · type a node in the filter to drill into its ports"}</span></div>`;
 const body=apps.length?`<div class="netgrid">`+apps.map(a=>{
  const peak=Math.max(0,...a.series),avg=d.buckets?a.total/d.buckets:0,dn=node?` data-node="${esc(node)}"`:"";
  return `<div class="netcard"${dn}><div class="nc-h"><span class="nc-app">${esc(a.app)}</span>`
   +`<span class="nc-rate">${fmtKB(peak)}/s peak</span></div>${netSpark(a.series)}`
   +`<div class="nc-sub">:${a.port} · ${fmtKB(avg)}/s avg</div></div>`;
 }).join("")+`</div>`:`<div class="empty">no port traffic in this window (is the ports probe deployed?)</div>`;
 viewEl("net").innerHTML=hdr+body;
}
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
[viewEl("table"),viewEl("rank"),viewEl("heat"),viewEl("net"),viewEl("risk"),viewEl("logs"),viewEl("svc"),viewEl("cost")].forEach(nodeClick);
// logs: severity / kind / source filters + sortable headers (delegated, all client-side so they
// survive innerHTML re-renders and never refetch). The #q node filter + #logq text both call drawLogs.
viewEl("logs").addEventListener("click",e=>{
 const sv=e.target.closest("[data-lsev]");if(sv){logFilter.sev=sv.dataset.lsev;drawLogs();return;}
 const kd=e.target.closest("[data-lkind]");if(kd){const k=kd.dataset.lkind;
  logFilter.kinds.has(k)?logFilter.kinds.delete(k):logFilter.kinds.add(k);drawLogs();return;}
 const sc=e.target.closest("[data-lsrc]");if(sc){const s=sc.dataset.lsrc;
  logFilter.sources.has(s)?logFilter.sources.delete(s):logFilter.sources.add(s);drawLogs();return;}
 const th=e.target.closest("[data-lsort]");if(th){const k=th.dataset.lsort;const so=logFilter.sort;
  if(so.key===k)so.dir*=-1;else{so.key=k;so.dir=(k==="node"||k==="source")?1:-1;}drawLogs();}});
viewEl("logs").addEventListener("input",e=>{const s=e.target.closest("#logq");
 if(s){logFilter.text=s.value;drawLogs();}});
let filtQT=null;  // #q drives the node filter on logs + the fleet/node drill-in on network
q.addEventListener("input",()=>{if(view!=="logs"&&view!=="net"&&view!=="risk")return;
 clearTimeout(filtQT);filtQT=setTimeout(()=>{if(view==="logs")loadLogs();else if(view==="net")loadNet();else if(view==="risk")drawRisk();},250);});
// risk filter bar (delegated; lives outside the [data-node] cards so it never opens the modal)
viewEl("risk").addEventListener("click",e=>{
 const sv=e.target.closest("[data-rsev]");if(sv){riskFilter.sev=sv.dataset.rsev;drawRisk();return;}
 const kd=e.target.closest("[data-rkind]");if(kd){const k=kd.dataset.rkind;
  riskFilter.kinds.has(k)?riskFilter.kinds.delete(k):riskFilter.kinds.add(k);drawRisk();return;}
 const mt=e.target.closest("[data-rmute]");if(mt){riskFilter.hideMuted=!riskFilter.hideMuted;drawRisk();}});
// "view logs →" inside the detail modal -> jump to the Logs tab filtered to that node.
// Delegated on document so it never depends on the modal element's declaration order.
document.addEventListener("click",e=>{const l=e.target.closest("[data-lognode]");if(l){gotoLogs(l.dataset.lognode);}});
function gotoLogs(node){detail.hidden=true;clearInterval(dTimer);q.value=node;setView("logs");loadLogs();}
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
 dimg=document.getElementById("dimg"),dmode=document.getElementById("dmode"),
 drisk=document.getElementById("drisk"),dports=document.getElementById("dports");
// render mode: png (matplotlib), plot (TUI plotext braille graphs), risks (per-node risk list)
// or ports (per-node connection counts). plot is the default; the rest are opt-in / contextual.
let dMode="plot";
dmode.innerHTML=[["png","png"],["plot","plot"],["risks","risks"],["ports","ports"]].map(([m,l])=>`<button data-m2="${m}">${l}</button>`).join("");
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
 const smoke=f.smoke_cpu_pct!=null?` · smoke ${Math.round(f.smoke_cpu_pct)}% cpu, ${Math.round(f.smoke_rss_mb||0)} MB`:"";
 dfoot.textContent=`shipped (measured ~24h): ${fmtKB(sd)}${pd?"/day":""} gzip · ${fmtK(f.rows_per_day!=null?f.rows_per_day:f.rows)} rows${pd?"/day":""}${f.ratio?" · "+f.ratio+":1 gzip":""}${smoke}${f.top&&f.top.length?" · top: "+f.top.map(t=>t.t).join(", "):""}`;}
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
function paintActive(){return dMode==="risks"?paintRisks():dMode==="ports"?paintPorts():dMode==="plot"?paintPlot():paintGraph();}
// ports tab: latest per-port connection counts for this node (/api/ports). Two columns:
// listening/inbound services and outbound remote-service ports, busiest-first.
async function paintPorts(){
 if(!dNode)return;
 dports.innerHTML='<div class="rd-load">loading ports…</div>';
 try{
  const r=await fetch("/api/ports?node="+encodeURIComponent(dNode),{cache:"no-store"});
  if(!r.ok){dports.innerHTML='<div class="empty">no port data</div>';return;}
  const d=await r.json();
  if(!d.ts){dports.innerHTML='<div class="empty">no port data for this node yet (ports probe not deployed here)</div>';return;}
  const kb=v=>v==null?"--":fmtKB(v);
  const tbl=rows=>rows.length?('<table class="pt-tbl"><thead><tr><th>proto</th><th>port</th><th>conns</th><th>peers</th><th>sent</th><th>recv</th></tr></thead><tbody>'
   +rows.map(p=>`<tr><td>${esc(p.proto)}</td><td class="pt-port">${p.port}</td><td class="${p.conns?"pt-hot":""}">${p.conns}</td><td>${p.peers}</td><td>${kb(p.bytes_sent)}</td><td class="${p.bytes_recv?"pt-hot":""}">${kb(p.bytes_recv)}</td></tr>`).join("")+'</tbody></table>'):'<div class="empty">none</div>';
  dports.innerHTML=`<div class="pt-cols">`
   +`<div><div class="rd-sec">listening / inbound <span>${d.listen.length}</span></div>${tbl(d.listen)}</div>`
   +`<div><div class="rd-sec">outbound (remote service ports) <span>${d.out.length}</span></div>${tbl(d.out)}</div></div>`;
 }catch(e){dports.innerHTML='<div class="empty">fetch error</div>';}
}
// risks tab: detailed per-node list of every death-clock / service alert / incident. Reuses the
// fleet /api/risks payload (cached ~15s) and filters to this node, so opening the tab is cheap.
let riskAll=null,riskTs=0;
async function loadRisksData(){
 if(riskAll&&Date.now()-riskTs<15000)return riskAll;
 try{const r=await fetch("/api/risks?hours=24",{cache:"no-store"});if(r.ok){riskAll=await r.json();riskTs=Date.now();}}catch(e){}
 return riskAll;}
async function paintRisks(){
 if(!dNode)return;
 drisk.innerHTML='<div class="rd-load">loading risks…</div>';
 const d=await loadRisksData();
 if(!d){drisk.innerHTML='<div class="empty">failed to load risks</div>';return;}
 const sev=s=>s===3?"critical":s===2?"warning":"watch";
 const row=(s,kind,detail,tail)=>`<div class="rd-row s${s}"><span class="rd-sev">${sev(s)}</span>`
  +`<span class="rd-kind">${esc(kind)}</span><span class="rd-detail">${esc(detail)}</span>`
  +`<span class="rd-tail">${tail?esc(tail):""}</span></div>`;
 // alert rows are richer: a summary headline + the context grid (cpu/mem/uptime/temp...) +
 // firing/muted delivery tags + a "view logs" deep-link when the kernel cause lives in logs.
 const kvgrid=ex=>(ex&&ex.length)?`<div class="rd-kv">`+ex.map(p=>`<span><b>${esc(p[0])}:</b> ${esc(String(p[1]))}</span>`).join("")+`</div>`:"";
 const arow=a=>{const tags=(a.since_s!=null?`<span class="pi-tag">firing ${esc(fmtDur(a.since_s))}</span>`:"")
   +(a.muted?`<span class="pi-tag muted">muted</span>`:a.notified?`<span class="pi-tag">paged</span>`:"")
   +(a.logs_hint?`<span class="rd-logs" data-lognode="${esc(a.node)}">view logs →</span>`:"");
  return `<div class="rd-row rd-alert s${a.severity}"><div class="rd-main"><span class="rd-sev">${sev(a.severity)}</span>`
   +`<span class="rd-kind">${esc(a.kind)}</span><span class="rd-detail">${esc(a.label||"")} · ${esc(a.summary||a.detail)}</span>`
   +`<span class="rd-tail">${tags}</span></div>${kvgrid(a.extra)}</div>`;};
 const cl=(d.clocks||[]).filter(x=>x.node===dNode).sort((a,b)=>(b.severity||1)-(a.severity||1));
 const al=(d.alerts||[]).filter(x=>x.node===dNode).sort((a,b)=>b.severity-a.severity);
 const inc=(d.incidents||[]).filter(x=>x.node===dNode).sort((a,b)=>b.start-a.start);
 const an=(d.anomalies||[]).filter(x=>x.node===dNode).sort((a,b)=>b.score-a.score);
 let h="";
 if(cl.length)h+='<div class="rd-sec">death clocks <span>'+cl.length+'</span></div>'
  +cl.map(c=>row(c.severity||1,c.kind,c.detail,c.eta_s!=null?"in "+fmtDur(c.eta_s):"")).join("");
 if(al.length)h+='<div class="rd-sec">service alerts <span>'+al.length+'</span></div>'
  +al.map(arow).join("");
 if(an.length)h+='<div class="rd-sec">anomalies <span>'+an.length+'</span></div>'
  +an.map(a=>row(a.severity||1,"anomaly",a.detail,tago(a.ts))).join("");
 if(inc.length)h+='<div class="rd-sec">recent incidents <span>'+inc.length+'</span></div>'
  +inc.map(i=>row(i.severity,i.klass,i.scope+" · "+i.detail,tago(i.start))).join("");
 drisk.innerHTML=h||'<div class="empty">no risks for this node — healthy</div>';}
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
function setMode(m){dMode=m;const graph=(m==="png"||m==="plot");
 dimg.hidden=(m!=="png");dplot.hidden=(m!=="plot");drisk.hidden=(m!=="risks");dports.hidden=(m!=="ports");
 // hours/cols/panels are graph-only -> hide them on the risks/ports tabs
 [dhours,dcols,dpanels].forEach(el=>el.style.display=graph?"":"none");
 syncCtl();paintActive();}
function openDetail(node){dNode=node;detail.hidden=false;
 // name + a status dot looked up from the latest fleet-status, built via safe DOM (no innerHTML)
 dname.textContent="";const fn=(last.nodes||[]).find(x=>x.node===node);
 if(fn){const dot=document.createElement("span");dot.className="st s-"+fn.state;dname.appendChild(dot);}
 dname.appendChild(document.createTextNode(node));
 dSel=null;dAvail=[];dpanels.innerHTML="";  // reset filter; the first (all) render relearns this node's panels
 dfoot.textContent="";loadFoot().then(renderFoot);  // footprint stat line under the graphs
 // opening from the risks tab -> start on the risks list; otherwise the graph (never auto-open
 // risks from a graph view, so fall back to plot if that was the last sticky mode).
 const startMode=(view==="risk")?"risks":(dMode==="risks"?"plot":dMode);
 dmsg.hidden=true;setMode(startMode);clearInterval(dTimer);dTimer=setInterval(paintActive,15000);}
function closeDetail(){detail.hidden=true;dNode=null;clearInterval(dTimer);dover.innerHTML="";freeBlob();dgraph.removeAttribute("src");}
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

async function sparkTick(){try{const r=await fetch("/api/spark?hours=2",{cache:"no-store"});if(r.ok){sparks=(await r.json()).spark||{};if(view==="table")render();else if(view==="grid")renderHosts();}}catch(e){}}
tick();setInterval(tick,REFRESH);
sparkTick();setInterval(sparkTick,30000);                 // sparklines: slow 2h trend (grid + table)
ingestTick();setInterval(ingestTick,REFRESH);             // live ingest gauge (grid)
setInterval(()=>{if(view==="grid")loadSvc().then(()=>{if(view==="grid")renderHosts();});},20000); // service badges (grid)
setInterval(()=>{if(view!=="grid")refreshView();},15000); // active non-grid view auto-refresh
setView("grid");
</script>
</body></html>
"""
