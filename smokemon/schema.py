"""Single source of truth for the SQLite schema (node-side and hub-side).

Each table's body columns are declared once; node DDL, hub DDL (adds node + src_id +
UNIQUE for idempotent ingest), STD_TABLES and the generic INSERT all derive from it.
Membership in _BODY is exactly "this table ships to the hub" -- node-local working state
(incident_state, signal_baseline, log_cursors) is deliberately declared by its owning module
instead, so the shipper never sees it."""

import sqlite3

from . import config

# Body = everything except `id INTEGER PRIMARY KEY` and the trailing node/src_id.
_BODY = {
    "ext_events": "ts REAL NOT NULL, source TEXT NOT NULL, severity TEXT, event TEXT NOT NULL, detail TEXT",
    "device_facts": "ts REAL NOT NULL, key TEXT NOT NULL, value TEXT, kind TEXT",
    # One row per STATE TRANSITION (open|reopen|close|stale|expired|persistent), never updated
    # in place. The shipper's monotonic rowid cursor makes an UPDATE to an already-shipped row
    # invisible to the hub forever, so the lifecycle is an append-only log keyed by uid and the
    # hub reduces rows per (node, uid). threshold/baseline/baseline_mad/z are stored AS
    # EVALUATED so an incident stays interpretable after a rule change -- the raw data that
    # would otherwise let you re-derive them no longer exists.
    "incidents": "ts REAL NOT NULL, uid TEXT NOT NULL, transition TEXT NOT NULL, "
                 "signal TEXT NOT NULL, entity TEXT, kind TEXT, rule TEXT, rule_hash TEXT, "
                 "detector_version INTEGER, schema_version INTEGER, severity TEXT, "
                 "value REAL, threshold REAL, baseline REAL, baseline_mad REAL, z REAL, "
                 "peak_mode TEXT, worst_value REAL, comparison_direction TEXT, "
                 "opened_ts REAL, duration_s REAL, n_samples INTEGER, detail TEXT",
    # Evidence samples around an incident. phase: pre (from the in-memory ring at trip time) |
    # during (decimated) | post (recovery tail). Joined to incidents on (node, uid), NOT on a
    # rowid FK: the hub does not remap ids for this table, so a rowid reference would be
    # meaningless there. A sample arriving before its parent is valid but unjoined.
    "incident_samples": "ts REAL NOT NULL, uid TEXT NOT NULL, phase TEXT NOT NULL, "
                        "signal TEXT NOT NULL, entity TEXT, value REAL",
    # Liveness + slow trend + detector self-observation. The only row a healthy node writes.
    # interval_s is carried IN the row so the hub derives staleness from what the node actually
    # does rather than from hub-side config -- one node may run a slower heartbeat without the
    # hub declaring it dead. agent_uptime_s is the only signal that catches a crash loop that
    # restarts faster than any rule's for_s. db_mb/wal_mb make "smokemon eats its own disk"
    # self-observable, which is the failure mode this design is most likely to introduce.
    "heartbeats": "ts REAL NOT NULL, interval_s REAL, uptime_s REAL, agent_uptime_s REAL, "
                  "db_mb REAL, wal_mb REAL, disk_free_gb REAL, disk_used_pct REAL, "
                  "inode_used_pct REAL, write_mb_day REAL, wear_pct REAL, "
                  "rss_mb REAL, cpu_pct REAL, load1 REAL, mem_used_pct REAL, "
                  "swap_used_pct REAL, temp_c REAL, throttle_bits INTEGER, "
                  "open_incidents INTEGER, signals INTEGER, signal_kb REAL, "
                  "signal_drops INTEGER, ver TEXT",
    # event-driven, capped, redacted log tails (opt-in). reason = what triggered the capture;
    # dropped = bytes skipped by the drop-oldest cap; excerpt = redacted text (wire-gzipped by
    # the shipper). Written only on incident, never as a stream.
    # uid links an excerpt to the incident that triggered it. NULL means unlinked evidence
    # (captured by a governor shed or probe crash with no incident open) -- readers must
    # tolerate that rather than inner-joining it away.
    "log_excerpts": "ts REAL NOT NULL, source TEXT NOT NULL, path TEXT, reason TEXT, "
                    "bytes INTEGER, dropped INTEGER, excerpt TEXT, uid TEXT",
}
_IX = {"ext_events": "source",
       "device_facts": "key", "log_excerpts": "source",
       "incidents": "signal", "incident_samples": "uid"}

STD_TABLES = tuple(_BODY)  # generic append-only tables (id + body + node [+ src_id])


def columns(table: str) -> list[str]:
    """Body column names (excludes id/node/src_id)."""
    return [c.split()[0] for c in _BODY[table].split(",")]


def _node_ddl() -> str:
    parts = []
    for t, body in _BODY.items():
        parts.append(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, {body}, node TEXT);")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_ts ON {t}(ts);")
        if t in _IX:
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_{_IX[t]}_ts ON {t}({_IX[t]}, ts);")
    return "\n".join(parts)


def _hub_ddl() -> str:
    parts = []
    for t, body in _BODY.items():
        parts.append(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, {body}, "
                     f"node TEXT, src_id INTEGER, UNIQUE(node, src_id));")
        # (node, ts): per-node time-range scans (the fleet/risks per-node loaders) + GROUP BY node.
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_node_ts ON {t}(node, ts);")
        # (ts): the dashboard runs many CROSS-node `WHERE ts >= ?` windows. Without a leading-ts
        # index those full-scan the table - a big reason a large hub DB makes GETs time out.
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_ts ON {t}(ts);")
        # (node, <entity>, ts): the "latest value per (node, entity)" queries - inventory's
        # current value per device fact, the events log per source - become a loose index scan
        # that jumps to each group's max-ts tail instead of scanning all history.
        if t in _IX:
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_node_{_IX[t]}_ts ON {t}(node, {_IX[t]}, ts);")
    # (uid, ts): the incident detail view loads evidence by uid ALONE, with no node predicate --
    # samples can arrive before their parent incident row, so the loader must not join. The
    # (node, uid, ts) index above cannot serve that: node leads, so SQLite falls back to scanning
    # every sample the hub has ever received. This is the index that query actually needs.
    parts.append("CREATE INDEX IF NOT EXISTS ix_incident_samples_uid_ts ON incident_samples(uid, ts);")
    return "\n".join(parts)


def _safe_add_column(conn: sqlite3.Connection, table: str, col_ddl: str) -> bool:
    """ALTER TABLE ADD COLUMN that tolerates a concurrent add of the same column.

    Two collector daemons (collect fast, collect slow) plus iperf open the same node
    DB and each run the migration at startup. On an in-place upgrade they all see the
    same columns missing and race the ALTERs; the loser would otherwise hit
    `OperationalError: duplicate column name`, which is SQLITE_ERROR (not SQLITE_BUSY),
    so busy_timeout does not retry it and the daemon would crash before its scheduler
    starts. Swallow only that specific error; re-raise anything else. Returns True when
    this call added the column, False when it was already present."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_ddl}")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        raise


def ensure_node_column(conn: sqlite3.Connection, tables=STD_TABLES) -> None:
    """Additive migration: add a `node` column to existing tables that lack it."""
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if cols and "node" not in cols and _safe_add_column(conn, t, "node TEXT"):
            conn.execute(f"UPDATE {t} SET node = ? WHERE node IS NULL", (config.NODE,))
    conn.commit()


def _body_cols(table: str) -> list[tuple[str, str]]:
    """Return [(name, full_ddl_fragment)] for each body column. Used by ensure_body_columns
    to detect and ALTER ADD missing columns on existing tables (additive migration)."""
    return [(c.strip().split()[0], c.strip()) for c in _BODY[table].split(",")]


def ensure_body_columns(conn: sqlite3.Connection, tables=STD_TABLES) -> None:
    """Additive migration: ALTER ADD any body columns missing from existing tables.
    Lets new columns (e.g. rtt_p25, rtt_p75) be introduced without breaking old DBs.
    Old rows keep NULL for the new columns; readers must tolerate that."""
    for t in tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        if not cols:
            continue
        for name, ddl in _body_cols(t):
            if name in cols:
                continue
            # Strip NOT NULL on retro-added cols since old rows have no value to backfill.
            ddl_safe = ddl.replace(" NOT NULL", "")
            _safe_add_column(conn, t, ddl_safe)
    conn.commit()


def init_node(conn: sqlite3.Connection) -> None:
    conn.executescript(_node_ddl())
    conn.commit()
    ensure_node_column(conn)
    ensure_body_columns(conn)


def init_hub(conn: sqlite3.Connection) -> None:
    conn.executescript(_hub_ddl())
    # Per-POST ingest accounting (hub-only): actual compressed bytes received on the wire per
    # node, so the dashboard can show real ship volume (not a from-the-DB estimate). wire_bytes
    # = Content-Length of the gzipped body; raw_bytes = decompressed JSON size.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS ingest_log ("
        "ts REAL NOT NULL, node TEXT, wire_bytes INTEGER, raw_bytes INTEGER, rows INTEGER);"
        "CREATE INDEX IF NOT EXISTS ix_ingest_log_ts ON ingest_log (ts);")
    # Alert-delivery flap-suppression state (hub-only, not shipped): one row per currently-firing
    # service alert, keyed by "node/kind/label". notified_ts drives the re-notify cooldown; rows are
    # deleted when the alert clears. Tiny (a handful of rows), so no extra index. See alerts.py.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS alert_state ("
        "key TEXT PRIMARY KEY, node TEXT, kind TEXT, label TEXT, severity INTEGER, "
        "detail TEXT, first_ts REAL, notified_ts REAL);")
    conn.commit()
    ensure_body_columns(conn)
    # Give the planner stats so it picks the loose-index scan over a full (node,ts) scan for the
    # latest-value queries. analysis_limit caps the sampling so this stays cheap even on a large
    # hub DB (a few hundred index rows per table, not a full ANALYZE).
    try:
        conn.execute("PRAGMA analysis_limit=400")
        conn.execute("PRAGMA optimize")
    except sqlite3.OperationalError as e:
        # optimize is advisory; never let a stats hiccup stop the hub from starting.
        import sys
        print(f"[schema] PRAGMA optimize skipped: {e!r}", file=sys.stderr)


_SQL_CACHE: dict[str, tuple[str, list[str]]] = {}


def _sql(table: str) -> tuple[str, list[str]]:
    """(insert_sql, body_cols), cached: _BODY is static so the SQL never changes.
    insert() is called several times per collect cycle, so skip re-parsing each time."""
    cached = _SQL_CACHE.get(table)
    if cached is None:
        cols = columns(table)
        sql = f"INSERT INTO {table} ({','.join(cols)},node) VALUES ({','.join('?' * (len(cols) + 1))})"
        cached = _SQL_CACHE[table] = (sql, cols)
    return cached


def insert(conn: sqlite3.Connection, table: str, rows: list[dict], node: str = config.NODE) -> None:
    """Generic INSERT of body columns + node. rows are dicts keyed by body column name."""
    sql, cols = _sql(table)
    conn.executemany(sql, [[r.get(c) for c in cols] + [node] for r in rows])


def insert_one(conn: sqlite3.Connection, table: str, row: dict, node: str = config.NODE) -> int:
    """INSERT a single row; return its rowid (used where a FK to it is needed, e.g. ping_runs)."""
    sql, cols = _sql(table)
    return conn.execute(sql, [row.get(c) for c in cols] + [node]).lastrowid
