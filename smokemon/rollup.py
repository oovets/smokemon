"""Hub-side downsampling (rollups). The hub stores raw samples (ping ~10s, host ~30-60s) for
the whole retention window; aggregate queries over long spans then scan millions of rows. This
module periodically aggregates the heavy time-series tables into lower-resolution `<table>_1m`
and `<table>_1h` tables so a days-long heatmap/ranking reads pre-aggregated buckets instead.

Guardrail: hub-side only, pure stdlib. The node is untouched - it still ships raw rows. Rollups
are additive (new tables + a rollup_state cursor) and derived entirely from real collected rows
(no fabricated values). Incremental: each pass only aggregates buckets that have fully closed
since the cursor, leaving the current open bucket until it closes. Idempotent: the rollup tables
carry UNIQUE(node, entity, bucket_ts) and we INSERT OR IGNORE, so a re-run never double-counts."""

import sqlite3
import time

from . import schema


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _cursor(conn: sqlite3.Connection, table: str, bucket: str) -> float:
    row = conn.execute("SELECT last_ts FROM rollup_state WHERE tbl=? AND bucket=?",
                       (table, bucket)).fetchone()
    return row[0] if row else 0.0


def _set_cursor(conn: sqlite3.Connection, table: str, bucket: str, last_ts: float) -> None:
    conn.execute("INSERT INTO rollup_state (tbl, bucket, last_ts) VALUES (?,?,?) "
                 "ON CONFLICT(tbl, bucket) DO UPDATE SET last_ts=excluded.last_ts",
                 (table, bucket, last_ts))


def _rollup_one(conn: sqlite3.Connection, table: str, suffix: str, size: int, now: float) -> int:
    """Aggregate closed buckets of `table` (since the cursor) into `table+suffix`. A bucket is
    closed when its end (bucket_ts + size) is <= the current bucket start, so the in-progress
    bucket is never rolled up early. Returns rows written."""
    rt = table + suffix
    if not _has_table(conn, table) or not _has_table(conn, rt):
        return 0
    entity = schema._IX.get(table)
    last_ts = _cursor(conn, table, suffix)  # bucket-start of the last bucket we already wrote
    open_bucket = now - (now % size)        # start of the still-open bucket; do not cross it
    # node + entity + the floored bucket start are the grouping keys.
    group_keys = ["node", f"CAST(ts / {size} AS INT) * {size} AS bucket_ts"]
    if entity:
        group_keys.insert(1, entity)
    sel = schema.rollup_select_cols(table)  # [(out_col, agg_expr)]
    out_cols = ["node"] + ([entity] if entity else []) + [c for c, _ in sel if c != entity] + ["bucket_ts"]
    agg_exprs = ["node"] + ([entity] if entity else []) \
        + [e for c, e in sel if c != entity] + [f"CAST(ts / {size} AS INT) * {size}"]
    group_by = "node" + (f", {entity}" if entity else "") + f", CAST(ts / {size} AS INT)"
    sql = (f"INSERT OR IGNORE INTO {rt} ({','.join(out_cols)}) "
           f"SELECT {','.join(agg_exprs)} FROM {table} "
           f"WHERE ts >= ? AND ts < ? GROUP BY {group_by}")
    cur = conn.execute(sql, (last_ts, open_bucket))
    written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    # Advance the cursor to the start of the open bucket: every bucket strictly before it is now
    # written, and re-reading from open_bucket next pass (with INSERT OR IGNORE) is safe.
    _set_cursor(conn, table, suffix, open_bucket)
    return written


def rollup(conn: sqlite3.Connection, now: float | None = None) -> dict[tuple[str, str], int]:
    """Build any missing closed buckets for every rollup table×bucket. Returns
    {(table, bucket): rows_written}. Commits once at the end; caller holds the hub write lock."""
    now = time.time() if now is None else now
    written: dict[tuple[str, str], int] = {}
    for table in schema.ROLLUP_TABLES:
        for suffix, size in schema.ROLLUP_BUCKETS.items():
            try:
                n = _rollup_one(conn, table, suffix, size, now)
            except sqlite3.OperationalError:
                continue  # table/columns not present (older hub mid-migration) -> skip
            if n:
                written[(table, suffix)] = n
    conn.commit()
    return written
