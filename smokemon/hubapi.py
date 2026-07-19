"""Hub read API: incidents, fleet liveness, evidence, and hub self-health.

Incident-first. The hub stores a transition log, not a time series, so nearly every view here
is a reduction over `incidents` joined to its evidence by `uid`. There are no charts of normal
operation, because normal operation is no longer recorded anywhere.

Two rules run through the whole module:

  * The absence of a close transition is NEVER read as "still broken". An open incident on a
    node whose heartbeat has gone stale means we do not know -- the node may have died mid
    incident and will never send its close. Those report as `unknown`, not `ongoing`.
  * Evidence is joined loosely. A sample or log excerpt whose parent incident has not arrived
    yet is held, not discarded: ship order is a latency optimisation, and correctness comes
    from uid being a content key rather than a foreign key.

Split out from hub.py so it can be unit-tested without a socket.
"""

from __future__ import annotations

import html
import time

from . import analyze, config, query

# Heartbeat multiples rather than absolute seconds: the node carries its own interval in the
# row, so a node deliberately running a slower heartbeat is not declared dead by a hub-side
# constant it has never heard of.
STALE_AFTER = 3.0
DEAD_AFTER = 12.0

_SEV_RANK = {"crit": 4, "critical": 4, "fatal": 4, "error": 3, "err": 3,
             "warn": 2, "warning": 2, "info": 1, "": 1}


def _rank(severity) -> int:
    """Unknown severities rank as warn rather than info: a severity we do not recognise is
    more likely a new elevated level than something safe to hide."""
    return _SEV_RANK.get(str(severity or "").strip().lower(), 2)


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 -- table absent on a partially-built hub
        return []


def nodes(conn) -> list[str]:
    """Every node the hub has heard from. Incidents and events count too, not just heartbeats:
    a node that has only ever sent incidents is one whose heartbeat is broken, and hiding it
    would hide exactly the failure worth seeing."""
    seen: set[str] = set()
    for table in ("heartbeats", "incidents", "ext_events"):
        seen |= {r[0] for r in _rows(conn, f"SELECT DISTINCT node FROM {table}") if r[0]}
    return sorted(seen)


# ---------- fleet liveness ----------

def _liveness(age_s: float | None, interval_s: float | None) -> str:
    if age_s is None:
        return "unknown"
    iv = interval_s or config.HEARTBEAT_INTERVAL
    if age_s > iv * DEAD_AFTER:
        return "dead"
    if age_s > iv * STALE_AFTER:
        return "stale"
    return "live"


def fleet(conn, now: float | None = None) -> list[dict]:
    """One row per node: liveness from the heartbeat, health from open incidents.

    `state` deliberately keeps "we know it is broken" separate from "we have stopped hearing
    from it". Collapsing those is how a monitoring system ends up reporting that everything is
    fine while a node is powered off."""
    now = time.time() if now is None else now
    out = []
    for node in nodes(conn):
        hb = query.latest_heartbeat(conn, node)
        age = (now - hb["ts"]) if hb else None
        live = _liveness(age, (hb or {}).get("interval_s"))
        open_incs = [i for i in query.load_incidents(conn, 0, now, node)
                     if i["state"] == "ongoing"]
        worst = max((_rank(i["severity"]) for i in open_incs), default=0)

        if live in ("dead", "unknown"):
            state = live
        elif open_incs:
            state = "critical" if worst >= 4 else "degraded"
        elif live == "stale":
            state = "stale"
        else:
            state = "ok"

        out.append({
            "node": node, "state": state, "liveness": live,
            "age_s": round(age, 1) if age is not None else None,
            "open_incidents": len(open_incs), "worst_severity": worst,
            # An open incident on a node we can no longer hear from is not evidence the fault
            # persists: the close transition may simply have nowhere to go.
            "open_trustworthy": live == "live",
            "heartbeat": hb,
        })
    order = {"dead": 0, "unknown": 1, "critical": 2, "degraded": 3, "stale": 4, "ok": 5}
    out.sort(key=lambda r: (order.get(r["state"], 9), -r["open_incidents"], r["node"]))
    return out


# ---------- incidents ----------

def incidents_feed(conn, hours: float = 24.0, node: str | None = None,
                   min_severity: int = 1, limit: int = 200,
                   now: float | None = None) -> dict:
    """The primary view: what broke, where, when, and whether it is still broken."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    live = {r["node"]: r["liveness"] for r in fleet(conn, now)}
    out = []
    for inc in query.load_incidents(conn, since, now, node):
        if _rank(inc["severity"]) < min_severity:
            continue
        if inc["state"] == "ongoing" and live.get(inc["node"]) != "live":
            # Open incident, no recent word from the node. Calling that "ongoing" would
            # present a guess as a fact.
            inc = {**inc, "state": "unknown", "unknown_reason": "node silent"}
        inc["severity_rank"] = _rank(inc["severity"])
        out.append(inc)
        if len(out) >= limit:
            break
    counts = {"total": len(out), "ongoing": 0, "unknown": 0, "closed": 0}
    for i in out:
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    return {"since": since, "until": now, "incidents": out, "counts": counts,
            "truncated": len(out) >= limit}


def incident_detail(conn, uid: str, now: float | None = None) -> dict | None:
    """One incident with the window captured around it.

    The samples are the whole point: baseline before the anomaly began, the onset, a decimated
    middle, and the recovery tail -- what an operator needs to judge it, and nothing about the
    hours of normal operation either side."""
    if not uid:
        return None
    now = time.time() if now is None else now
    found = query.load_incidents(conn, 0, now, uid=uid)
    if not found:
        return None
    inc = found[0]
    # Same silent-node rule as incidents_feed. It has to be applied here too: the detail view
    # is reached directly by uid, so without this one incident could read "ongoing" on the
    # page and "unknown" in the feed -- and the more emphatic of the two would be the guess.
    live = {r["node"]: r["liveness"] for r in fleet(conn, now)}
    if inc["state"] == "ongoing" and live.get(inc["node"]) != "live":
        inc = {**inc, "state": "unknown", "unknown_reason": "node silent"}
    samples = query.load_incident_samples(conn, uid)
    phases: dict[str, list] = {"pre": [], "during": [], "post": []}
    for s in samples:
        phases.setdefault(s["phase"], []).append(s)
    evidence = [{"ts": t, "source": s, "path": p, "reason": r, "bytes": b,
                 "dropped": d, "excerpt": x}
                for (t, s, p, r, b, d, x) in _rows(
                    conn, "SELECT ts,source,path,reason,bytes,dropped,excerpt "
                          "FROM log_excerpts WHERE uid=? ORDER BY ts", (uid,))]
    return {**inc, "samples": samples, "phases": phases, "evidence": evidence}


def incident_density(conn, hours: float = 168.0, now: float | None = None) -> dict:
    """Incidents per node per hour. Replaces the old loss heatmap.

    The old grid coloured cells by measured loss, which made an hour with no samples
    indistinguishable from an hour that was fine -- it answered "where do we have data" while
    appearing to answer "where was it bad". Counting incidents inverts that correctly: an empty
    cell means nothing happened, which is exactly what the node now records."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    hour0 = int(since // 3600) * 3600
    n = int((now - hour0) // 3600) + 1
    counts: dict[str, list] = {}
    worst: dict[str, list] = {}
    for inc in query.load_incidents(conn, since, now):
        row = counts.setdefault(inc["node"], [0] * n)
        wrow = worst.setdefault(inc["node"], [0] * n)
        # An incident occupies every hour it spanned, not only the one it opened in: a six-hour
        # outage rendered as a single cell reads as a blip.
        start = max(inc["opened_ts"], since)
        end = min(inc["ended_ts"] or now, now)
        for b in range(max(0, int((start - hour0) // 3600)),
                       min(n, int((end - hour0) // 3600) + 1)):
            row[b] += 1
            wrow[b] = max(wrow[b], _rank(inc["severity"]))
    return {"hour0": hour0, "buckets": n, "counts": counts, "worst": worst,
            "nodes": sorted(counts)}


def open_incident_alerts(conn, now: float | None = None) -> dict[str, dict]:
    """Currently-open incidents in the shape alerts.py wants, keyed by uid.

    A projection, not a second evaluation: the detector already did debounce, hysteresis,
    cooldown and dedup on the node. uid is also a better alert key than the old
    node/kind/label triple -- it is stable for the life of one occurrence and changes when a
    genuinely new occurrence begins, which is exactly what a re-notify cooldown needs.

    Incidents on nodes we cannot currently hear from are excluded: paging someone about a
    fault we are no longer receiving updates for produces an alert that can never clear."""
    now = time.time() if now is None else now
    trustworthy = {r["node"]: r["liveness"] == "live" for r in fleet(conn, now)}
    out = {}
    for inc in query.load_incidents(conn, 0, now):
        if inc["state"] != "ongoing" or not trustworthy.get(inc["node"]):
            continue
        where = f"{inc['signal']}/{inc['entity']}" if inc["entity"] else inc["signal"]
        detail = f"{where} for {analyze._dur(now - inc['opened_ts'])}"
        if inc["worst_value"] is not None:
            detail += f", worst {inc['worst_value']:g}"
        out[inc["uid"]] = {"key": inc["uid"], "node": inc["node"], "kind": inc["signal"],
                           "label": inc["entity"] or "-", "severity": _rank(inc["severity"]),
                           "detail": detail}
    return out


# ---------- evidence and events ----------

_QUIET_SQL = "('','info','debug','notice','trace')"


def events_log(conn, node=None, severity="elevated", hours: float = 24.0,
               limit: int = 200, now: float | None = None) -> dict:
    """ext_events plus captured log excerpts. Both are already sparse by design."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    nf, npar = query._filt(node)
    # The severity predicate goes in SQL, never as a post-LIMIT filter in Python: filtering
    # after the limit meant that when the fleet was flapping hardest, the newest rows were all
    # recovery noise and the elevated view came back empty -- blank exactly when it mattered.
    sev_sql = ""
    if severity == "elevated":
        sev_sql = f" AND LOWER(COALESCE(severity,'')) NOT IN {_QUIET_SQL}"
    events = [{"ts": t, "node": n, "source": s, "severity": sv, "event": e,
               "detail": d, "rank": _rank(sv)}
              for (t, n, s, sv, e, d) in _rows(
                  conn, "SELECT ts,node,source,severity,event,detail FROM ext_events "
                        "WHERE ts>=?" + nf + sev_sql + " ORDER BY ts DESC LIMIT ?",
                  [since, *npar, limit])]
    excerpts = [{"ts": t, "node": n, "source": s, "path": p, "reason": r,
                 "bytes": b, "dropped": d, "uid": u}
                for (t, n, s, p, r, b, d, u) in _rows(
                    conn, "SELECT ts,node,source,path,reason,bytes,dropped,uid "
                          "FROM log_excerpts WHERE ts>=?" + nf + " ORDER BY ts DESC LIMIT ?",
                    [since, *npar, limit])]
    return {"since": since, "events": events, "excerpts": excerpts}


def inventory(conn, now: float | None = None) -> dict:
    """Current value of every delta-coded device fact, per node."""
    out: dict[str, dict] = {}
    for node, key, value, kind, ts in _rows(
            conn, "SELECT node,key,value,kind,MAX(ts) FROM device_facts "
                  "GROUP BY node,key ORDER BY node,key"):
        out.setdefault(node, {})[key] = {"value": value, "kind": kind, "ts": ts}
    return {"nodes": out}


# ---------- hub self-health ----------

def hub_health(conn, now: float | None = None) -> dict:
    """What the hub knows about its own trustworthiness.

    orphan_samples is a first-class metric rather than a curiosity: samples outrunning their
    parent is normal and transient, but a count that stays high means incidents are being lost
    between the node and here, and every incident view is then quietly incomplete."""
    now = time.time() if now is None else now
    orphans, oldest = query.orphan_stats(conn, now)
    rows = {}
    for t in ("incidents", "incident_samples", "heartbeats", "ext_events", "log_excerpts"):
        got = _rows(conn, f"SELECT COUNT(*) FROM {t}")
        rows[t] = got[0][0] if got else None
    return {"orphan_samples": orphans, "oldest_orphan_s": oldest, "rows": rows, "now": now}


def ship_volume(conn, hours: float = 24.0, now: float | None = None) -> dict:
    """Per-node ingest volume from the hub's own per-POST accounting."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    per = [{"node": n, "posts": p, "wire_bytes": w or 0, "raw_bytes": r or 0, "rows": rw or 0}
           for (n, p, w, r, rw) in _rows(
               conn, "SELECT node, COUNT(*), SUM(wire_bytes), SUM(raw_bytes), SUM(rows) "
                     "FROM ingest_log WHERE ts>=? GROUP BY node "
                     "ORDER BY SUM(wire_bytes) DESC", (since,))]
    total = sum(p["wire_bytes"] for p in per)
    return {"since": since, "hours": hours, "nodes": per, "wire_bytes": total,
            "per_day_bytes": round(total * 24.0 / hours) if hours else 0}


def ingest_rate(events, now: float | None = None, window_s: float = 900.0) -> dict:
    """Recent ingest throughput from the hub's in-memory ring (no table involved)."""
    now = time.time() if now is None else now
    recent = [e for e in events if now - e[0] <= window_s]
    if not recent:
        return {"posts": 0, "wire_bps": 0.0, "rows_per_s": 0.0, "window_s": window_s}
    span = max(1.0, now - recent[0][0])
    return {"posts": len(recent),
            "wire_bps": round(sum(e[1] for e in recent) / span, 1),
            "rows_per_s": round(sum(e[3] for e in recent) / span, 2),
            "window_s": window_s}


# ---------- prometheus ----------

def _metric(name, help_text, typ, samples) -> list[str]:
    if not samples:
        return []
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}"]
    out += [f"{name}{{{labels}}} {value}" if labels else f"{name} {value}"
            for labels, value in samples]
    return out


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def prometheus(conn, now: float | None = None) -> str:
    """Exposition built from liveness and incidents.

    There are no per-signal gauges any more. The node no longer ships a time series, and
    synthesising one here from incident windows would export a chart made only of the bad
    moments -- worse than exporting nothing, because it would look complete."""
    now = time.time() if now is None else now
    live, openi, age = [], [], []
    for r in fleet(conn, now):
        lbl = f'node="{_esc(r["node"])}"'
        live.append((lbl, 1 if r["liveness"] == "live" else 0))
        openi.append((lbl, r["open_incidents"]))
        if r["age_s"] is not None:
            age.append((lbl, r["age_s"]))
    lines = _metric("smokemon_node_live", "1 when the node's heartbeat is fresh", "gauge", live)
    lines += _metric("smokemon_open_incidents", "Currently-open incidents", "gauge", openi)
    lines += _metric("smokemon_heartbeat_age_seconds", "Seconds since the last heartbeat",
                     "gauge", age)
    lines += _metric("smokemon_orphan_samples",
                     "Incident samples whose parent transition has not arrived yet", "gauge",
                     [("", hub_health(conn, now)["orphan_samples"])])
    return "\n".join(lines) + "\n"


# ---------- dashboard ----------

def dashboard_html() -> str:
    """The dashboard is a static asset, not a string constant in this module.

    It used to be 1612 lines of embedded HTML/CSS/JS -- 59% of this file, with no syntax
    checking, no linting, and tests that could only assert substrings against it.

    Read through importlib.resources rather than Path(__file__): the agent ships as a zipapp,
    where __file__ points inside the archive and open() cannot follow it. The fallback below
    would then be served instead of the dashboard, and because it is also valid HTML that
    failure looks like a working page until you read the words."""
    try:
        from importlib import resources
        return (resources.files("smokemon") / "static" / "dashboard.html").read_text("utf-8")
    except (OSError, ModuleNotFoundError, AttributeError):
        return ("<!doctype html><meta charset=utf-8><title>smokemon</title>"
                "<p>dashboard.html is missing from this install.")


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


# Favicon: the brand sparkline on the dashboard's dark rounded tile, so the browser tab matches
# the header and /favicon.ico stops 404ing.
FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="32" height="32">'
    b'<rect width="24" height="24" rx="5" fill="#0b0e14"/>'
    b'<path d="M2 12h3.5l2-7 4 15 3-10 1.5 3H22" fill="none" stroke="#58a6ff" stroke-width="2" '
    b'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
