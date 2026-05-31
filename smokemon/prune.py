"""Retention / pruning for the node DB. Without this the append-only tables grow forever,
the WAL balloons, and an SD card wears out from write amplification. Read-light, stdlib-only.

Safety rule: a row is deleted only when it is BOTH older than RETENTION_DAYS AND already
shipped (id <= the ship_state cursor) when a hub is configured. So a long hub outage backs up
on disk rather than silently dropping un-shipped data. With no hub configured, age alone applies
(the data is local-only and nothing is waiting to be sent).

Reclaiming space: SQLite reuses freed pages for later inserts, so the main file stops growing
after a prune even without VACUUM. The WAL is the part that grows unbounded between checkpoints,
so we always `wal_checkpoint(TRUNCATE)` it back down. SMOKEMON_PRUNE_VACUUM=1 additionally runs a
full VACUUM to hand pages back to the filesystem (heavier; needs transient free space).

Run once from a timer: `python -m smokemon.prune` (see deploy/).
"""

import sqlite3
import sys
import time

from . import config, core, schema


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _shipped_last(conn: sqlite3.Connection, table: str) -> int:
    """The ship_state cursor for `table` (0 if never shipped / no ship_state)."""
    try:
        row = conn.execute("SELECT last_id FROM ship_state WHERE table_name=?", (table,)).fetchone()
    except sqlite3.OperationalError:
        return 0
    return row[0] if row else 0


def prune(conn: sqlite3.Connection, now: float | None = None,
          retention_days: float | None = None, require_shipped: bool | None = None) -> dict[str, int]:
    """Delete shipped rows older than the retention window. Returns {table: rows_deleted}.
    Does not checkpoint/vacuum - main() does that after, so the function stays pure/testable."""
    now = time.time() if now is None else now
    retention_days = config.RETENTION_DAYS if retention_days is None else retention_days
    if retention_days <= 0:
        return {}  # pruning disabled
    if require_shipped is None:
        require_shipped = bool(config.HUB_URL)
    cutoff = now - retention_days * 86400.0
    deleted: dict[str, int] = {}

    for t in schema.STD_TABLES:
        if not _has_table(conn, t):
            continue
        if require_shipped:
            safe_id = _shipped_last(conn, t)
            cur = conn.execute(f"DELETE FROM {t} WHERE ts < ? AND id <= ?", (cutoff, safe_id))
        else:
            cur = conn.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
        if cur.rowcount and cur.rowcount > 0:
            deleted[t] = cur.rowcount

    # ping_rtts has no ts of its own; it hangs off ping_runs.id. ping_runs ids rise with ts,
    # so after pruning ping_runs the survivors are a contiguous high range - drop every rtt
    # whose parent run is gone (run_id below the smallest surviving run id).
    if _has_table(conn, "ping_rtts") and _has_table(conn, "ping_runs"):
        row = conn.execute("SELECT MIN(id) FROM ping_runs").fetchone()
        min_run = row[0] if row else None
        if min_run is None:
            cur = conn.execute("DELETE FROM ping_rtts")
        else:
            cur = conn.execute("DELETE FROM ping_rtts WHERE run_id < ?", (min_run,))
        if cur.rowcount and cur.rowcount > 0:
            deleted["ping_rtts"] = cur.rowcount

    conn.commit()
    return deleted


def main() -> int:
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    if config.RETENTION_DAYS <= 0:
        core.log("prune: SMOKEMON_RETENTION_DAYS<=0, pruning disabled")
        conn.close()
        return 0
    deleted = prune(conn)
    total = sum(deleted.values())
    if config.PRUNE_VACUUM:
        conn.execute("VACUUM")  # must run outside a transaction; prune() already committed
    # Truncate the WAL back to zero so the on-disk footprint actually drops after a big prune.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as e:
        core.log(f"prune: wal_checkpoint failed: {e!r}")
    conn.close()
    detail = ", ".join(f"{t}={n}" for t, n in sorted(deleted.items(), key=lambda kv: -kv[1])) or "nothing"
    core.log(f"prune: deleted {total} rows ({detail}) older than {config.RETENTION_DAYS}d "
             f"(require_shipped={bool(config.HUB_URL)}, vacuum={config.PRUNE_VACUUM})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
