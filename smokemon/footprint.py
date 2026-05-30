"""Collector footprint estimates from a smokemon SQLite DB.

This is read-only and stdlib-only. It measures rows produced in a time window,
normalises that to a per-day rate, and estimates shipper wire bytes by encoding
the same compact JSON+gzip shape that smokemon.ship posts to the hub.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from dataclasses import dataclass

from . import schema


SECONDS_PER_DAY = 86400.0


@dataclass
class TableFootprint:
    table: str
    rows: int
    rows_per_day: float


@dataclass
class Footprint:
    db: str
    node: str | None
    since: float
    until: float
    observed_span_s: float
    tables: list[TableFootprint]
    raw_rtts: TableFootprint
    collector_rows: int
    collector_rows_per_day: float
    ship_rows: int
    ship_json_bytes: int
    ship_gzip_bytes: int
    ship_gzip_bytes_per_day: float
    sqlite_bytes: int
    sqlite_bytes_per_day: float | None
    ship_rtts: bool


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.OperationalError:
        return False


def _node_filter(conn: sqlite3.Connection, table: str, node: str | None) -> tuple[str, list]:
    if node and _has_column(conn, table, "node"):
        return " AND node=?", [node]
    return "", []


def _window_count(conn: sqlite3.Connection, table: str, since: float, until: float,
                  node: str | None) -> tuple[int, float | None, float | None]:
    if not _has_table(conn, table):
        return 0, None, None
    nf, np_ = _node_filter(conn, table, node)
    return conn.execute(
        f"SELECT count(*), min(ts), max(ts) FROM {table} WHERE ts BETWEEN ? AND ?" + nf,
        [since, until, *np_],
    ).fetchone()


def _rtt_count(conn: sqlite3.Connection, since: float, until: float, node: str | None) -> int:
    if not (_has_table(conn, "ping_rtts") and _has_table(conn, "ping_runs")):
        return 0
    nf, np_ = _node_filter(conn, "ping_runs", node)
    row = conn.execute(
        "SELECT count(*) FROM ping_rtts r JOIN ping_runs p ON p.id=r.run_id "
        "WHERE p.ts BETWEEN ? AND ?" + nf,
        [since, until, *np_],
    ).fetchone()
    return int(row[0]) if row else 0


def _rows_per_day(rows: int, span_s: float) -> float:
    return rows * SECONDS_PER_DAY / span_s if rows and span_s > 0 else 0.0


def _file_bytes(db: str) -> int:
    total = 0
    for suffix in ("", "-wal"):
        path = db + suffix
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


def _full_span(conn: sqlite3.Connection, node: str | None) -> float:
    lows: list[float] = []
    highs: list[float] = []
    for table in schema.STD_TABLES:
        if not _has_table(conn, table):
            continue
        nf, np_ = _node_filter(conn, table, node)
        mn, mx = conn.execute(f"SELECT min(ts), max(ts) FROM {table} WHERE ts IS NOT NULL" + nf, np_).fetchone()
        if mn is not None and mx is not None:
            lows.append(mn)
            highs.append(mx)
    if not lows or not highs:
        return 0.0
    return max(0.0, max(highs) - min(lows))


def _ship_payload(conn: sqlite3.Connection, since: float, until: float, node: str | None,
                  ship_rtts: bool) -> tuple[dict, dict[str, int]]:
    payload: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for table in schema.STD_TABLES:
        if not _has_table(conn, table):
            continue
        nf, np_ = _node_filter(conn, table, node)
        table_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "src_id" in table_cols:
            body = [c for c in schema.columns(table) if c in table_cols]
            cols = ["id", *body]
            if "node" in table_cols:
                cols.append("node")
            select = ["src_id AS id", *body] + (["node"] if "node" in table_cols else [])
            cur = conn.execute(
                f"SELECT {','.join(select)} FROM {table} WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY src_id",
                [since, until, *np_],
            )
        else:
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY id",
                [since, until, *np_],
            )
            cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        if rows:
            payload[table] = {"columns": cols, "rows": rows}
            counts[table] = len(rows)

    if ship_rtts and _has_table(conn, "ping_rtts") and _has_table(conn, "ping_runs"):
        nf, np_ = _node_filter(conn, "ping_runs", node)
        rows = conn.execute(
            "SELECT r.run_id, r.rtt_ms FROM ping_rtts r JOIN ping_runs p ON p.id=r.run_id "
            "WHERE p.ts BETWEEN ? AND ?" + nf + " ORDER BY r.run_id",
            [since, until, *np_],
        ).fetchall()
        if rows:
            payload["ping_rtts"] = {"columns": ["run_id", "rtt_ms"], "rows": [list(r) for r in rows]}
            counts["ping_rtts"] = len(rows)
    return payload, counts


def analyze(conn: sqlite3.Connection, db: str, since: float, until: float, node: str | None = None,
            ship_rtts: bool = False) -> Footprint:
    tables: list[TableFootprint] = []
    mins: list[float] = []
    maxs: list[float] = []
    for table in schema.STD_TABLES:
        rows, mn, mx = _window_count(conn, table, since, until, node)
        if mn is not None and mx is not None:
            mins.append(mn)
            maxs.append(mx)
        tables.append(TableFootprint(table, int(rows), 0.0))

    observed_span = max(0.0, max(maxs) - min(mins)) if mins and maxs else 0.0
    tables = [TableFootprint(t.table, t.rows, _rows_per_day(t.rows, observed_span)) for t in tables]

    raw_rtt_rows = _rtt_count(conn, since, until, node)
    raw_rtts = TableFootprint("ping_rtts", raw_rtt_rows, _rows_per_day(raw_rtt_rows, observed_span))
    collector_rows = sum(t.rows for t in tables) + raw_rtt_rows
    payload, ship_counts = _ship_payload(conn, since, until, node, ship_rtts)
    body = {"node": node or "estimate", "tables": payload}
    raw = json.dumps(body, separators=(",", ":")).encode()
    gz = gzip.compress(raw, compresslevel=3)
    full_span = _full_span(conn, node)
    sqlite_bytes = _file_bytes(db)
    sqlite_bpd = sqlite_bytes * SECONDS_PER_DAY / full_span if sqlite_bytes and full_span > 0 else None
    return Footprint(
        db=db,
        node=node,
        since=since,
        until=until,
        observed_span_s=observed_span,
        tables=tables,
        raw_rtts=raw_rtts,
        collector_rows=collector_rows,
        collector_rows_per_day=_rows_per_day(collector_rows, observed_span),
        ship_rows=sum(ship_counts.values()),
        ship_json_bytes=len(raw),
        ship_gzip_bytes=len(gz),
        ship_gzip_bytes_per_day=_rows_per_day(len(gz), observed_span),
        sqlite_bytes=sqlite_bytes,
        sqlite_bytes_per_day=sqlite_bpd,
        ship_rtts=ship_rtts,
    )


def _human_bytes(n: float | None) -> str:
    if n is None:
        return "n/a"
    units = ("B", "KB", "MB", "GB", "TB")
    val = float(n)
    for unit in units:
        if abs(val) < 1024 or unit == units[-1]:
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _human_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < SECONDS_PER_DAY:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / SECONDS_PER_DAY:.1f}d"


def render(fp: Footprint, limit: int = 8) -> str:
    node = fp.node or "all"
    lines = [
        f"footprint: db={fp.db} node={node} window={_human_duration(fp.until - fp.since)} "
        f"observed={_human_duration(fp.observed_span_s)}",
        f"collector: {fp.collector_rows} rows in window, {fp.collector_rows_per_day:.0f} rows/day",
        f"ship estimate: {fp.ship_rows} rows, {_human_bytes(fp.ship_gzip_bytes)} gzip "
        f"({_human_bytes(fp.ship_gzip_bytes_per_day)}/day), {_human_bytes(fp.ship_json_bytes)} json",
        f"sqlite on disk: {_human_bytes(fp.sqlite_bytes)} now"
        + (f", ~{_human_bytes(fp.sqlite_bytes_per_day)}/day over DB span"
           if fp.sqlite_bytes_per_day is not None else ""),
    ]
    if not fp.ship_rtts and fp.raw_rtts.rows:
        lines.append(f"raw ping RTTs: {fp.raw_rtts.rows} rows local-only "
                     f"({fp.raw_rtts.rows_per_day:.0f}/day); add --ship-rtts to estimate shipping them")
    elif fp.ship_rtts and fp.raw_rtts.rows:
        lines.append(f"raw ping RTTs: included in ship estimate ({fp.raw_rtts.rows} rows, "
                     f"{fp.raw_rtts.rows_per_day:.0f}/day)")

    nonzero = [t for t in fp.tables if t.rows]
    nonzero.sort(key=lambda t: t.rows_per_day, reverse=True)
    if nonzero:
        lines.append("")
        lines.append("top tables:")
        name_w = max(len(t.table) for t in nonzero[:limit])
        for t in nonzero[:limit]:
            lines.append(f"  {t.table:<{name_w}}  {t.rows:>7} rows  {t.rows_per_day:>9.0f}/day")
    return "\n".join(lines)
