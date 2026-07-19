"""Shared read-side: time window + the incident-centric loaders the hub reads.
All loaders return raw epoch-second timestamps and accept an optional node filter
(required when reading a hub DB that holds multiple nodes)."""

import sqlite3
import time
from datetime import datetime
from urllib.parse import urlparse

from . import schema

# Shown when a viewer finds no DB yet, so first-run users know the next step.
COLLECT_HINT = ("Nothing collected yet.  systemctl status smokemon   |   journalctl -u smokemon -f\n"
                "  A healthy node writes only a heartbeat, so give it one interval (5 min) before"
                " expecting rows.")


def window(hours: float, minutes: float | None, since: str | None, until: str | None) -> tuple[float, float]:
    u = datetime.fromisoformat(until).timestamp() if until else datetime.now().timestamp()
    if since:
        s = datetime.fromisoformat(since).timestamp()
    elif minutes is not None:
        s = u - minutes * 60
    else:
        s = u - hours * 3600
    return s, u


def host_label(url: str) -> str:
    h = urlparse(url).netloc.replace("www.", "")
    return h.rsplit(".", 1)[0] if "." in h else (h or url)  # strip domain suffix (.com etc.)


def last_value(seq):
    """Most recent non-None value in a time-ordered sequence, or None. Used for the
    'current' annotation on any series a caller has already assembled."""
    return next((v for v in reversed(seq) if v is not None), None)


def _q(conn, sql: str, params):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _filt(node: str | None) -> tuple[str, list]:
    return (" AND node=?", [node]) if node else ("", [])


def open_ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def load_ext_events(conn, since, until, node=None, limit: int = 20):
    """External events (scrape failures, non-2xx/3xx statuses), newest last."""
    nf, np_ = _filt(node)
    rows = _q(conn, "SELECT ts,source,severity,event,detail FROM ext_events "
              "WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts DESC LIMIT ?",
              [since, until, *np_, limit])
    return [{"ts": ts, "source": source, "severity": severity, "event": event, "detail": detail}
            for ts, source, severity, event, detail in reversed(rows)]


# ---------- incident-centric loaders ----------

# A terminal transition ends an occurrence. Anything else (open / reopen / persist) leaves it
# running, so `state` is decided by which of the two came last -- not by whether a close exists
# at all, because a reopen legitimately follows a close within the same uid.
_TERMINAL = ("close", "stale", "expired")


def load_incidents(conn, since, until, node=None) -> list[dict]:
    """One dict per incident, reduced from the append-only transition rows in `incidents`.

    The table is a log keyed by (node, uid), never updated in place, so every read has to do
    this reduction; there is no "current row" to select. Newest first. An incident whose open
    predates `since` is still returned from whatever transitions fall in the window -- with
    opened_ts recovered from the column the node stamps on every row, so a window that catches
    only the close still reports the true start."""
    nf, np_ = _filt(node)
    rows = _q(conn, "SELECT node,uid,ts,transition,signal,entity,severity,worst_value,"
              "opened_ts,duration_s,threshold,baseline,baseline_mad,z,detail,rule_hash "
              "FROM incidents WHERE ts BETWEEN ? AND ?" + nf
              + " ORDER BY ts", [since, until, *np_])
    out: dict[tuple, dict] = {}
    for (n, uid, ts, transition, signal, entity, severity, worst, opened, dur,
         thresh, base, mad, z, detail, rhash) in rows:
        inc = out.get((n, uid))
        if inc is None:
            inc = out[(n, uid)] = {
                "uid": uid, "node": n, "signal": signal, "entity": entity,
                "severity": None, "opened_ts": opened if opened is not None else ts,
                "ended_ts": None, "duration_s": None, "worst_value": None,
                "threshold": None, "baseline": None, "baseline_mad": None, "z": None,
                "detail": None, "rule_hash": rhash,
                "transitions": [], "state": "ongoing"}
        inc["transitions"].append({"ts": ts, "transition": transition})
        if opened is not None:
            inc["opened_ts"] = opened
        # Terminal rows carry severity 'info' (they report the end, not the fault), so keep the
        # severity the opening transition evaluated -- otherwise every closed incident reads as info.
        if transition not in _TERMINAL:
            inc["severity"] = severity
            # Likewise for the evaluation context. threshold/baseline/z are stored as they were
            # AT THE MOMENT the rule fired precisely so the incident stays interpretable after a
            # threshold change; a terminal row carries the clear threshold instead, and letting
            # that overwrite would make every resolved incident look like it tripped at the
            # wrong number.
            inc["threshold"] = thresh
            inc["baseline"] = base
            inc["baseline_mad"] = mad
            inc["z"] = z
            inc["detail"] = detail
        elif inc["severity"] is None:
            inc["severity"] = severity
        if worst is not None:
            inc["worst_value"] = worst
        if dur is not None:
            inc["duration_s"] = dur
        if transition in _TERMINAL:
            inc["ended_ts"] = ts
            inc["state"] = "closed"
        else:
            inc["ended_ts"] = None
            inc["state"] = "ongoing"
    for inc in out.values():
        if inc["duration_s"] is None and inc["ended_ts"] is not None:
            inc["duration_s"] = inc["ended_ts"] - inc["opened_ts"]
    return sorted(out.values(), key=lambda i: i["opened_ts"], reverse=True)


def load_incident_samples(conn, uid: str) -> list[dict]:
    """Evidence samples for one incident, oldest first. Deliberately queries incident_samples
    alone: samples can arrive before their parent transition row (the shipper orders tables for
    latency, not for referential integrity), and a join would hide exactly the rows that prove
    what happened."""
    return [{"ts": ts, "phase": phase, "signal": signal, "entity": entity, "value": value}
            for ts, phase, signal, entity, value in _q(
                conn, "SELECT ts,phase,signal,entity,value FROM incident_samples "
                      "WHERE uid=? ORDER BY ts", [uid])]


def load_heartbeats(conn, since, until, node=None) -> list[dict]:
    """Heartbeat rows in the window, oldest first, as plain dicts keyed by column name."""
    nf, np_ = _filt(node)
    cols = ["node", *schema.columns("heartbeats")]
    rows = _q(conn, f"SELECT {','.join(cols)} FROM heartbeats WHERE ts BETWEEN ? AND ?" + nf
              + " ORDER BY ts", [since, until, *np_])
    return [dict(zip(cols, r)) for r in rows]


def latest_heartbeat(conn, node: str) -> dict | None:
    """The node's newest heartbeat, or None when it has never reported. Staleness is derived
    from the row's own interval_s, so a node running a slower heartbeat is not called dead."""
    cols = ["node", *schema.columns("heartbeats")]
    rows = _q(conn, f"SELECT {','.join(cols)} FROM heartbeats WHERE node=? ORDER BY ts DESC LIMIT 1",
              [node])
    return dict(zip(cols, rows[0])) if rows else None


def orphan_stats(conn, now: float | None = None) -> tuple[int, float]:
    """(orphan_count, oldest_orphan_age_s) for incident_samples with no matching incidents row.

    A steady trickle of orphans is normal (samples ship ahead of their parent), a growing
    backlog of OLD ones is not: it means transition rows are being lost while their evidence
    arrives, which no other metric would reveal. Age 0.0 when there are none."""
    now = time.time() if now is None else now
    rows = _q(conn, "SELECT COUNT(*), MIN(s.ts) FROM incident_samples s WHERE NOT EXISTS ("
                    "SELECT 1 FROM incidents i WHERE i.uid = s.uid AND i.node IS s.node)", [])
    if not rows:
        return 0, 0.0
    count, oldest = rows[0]
    return int(count or 0), (now - oldest) if oldest is not None else 0.0
