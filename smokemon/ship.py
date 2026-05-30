"""Ship new rows (delta by ascending id) from the node's local DB to the hub's /ingest.
A local ship_state(table_name,last_id) cursor advances only on HTTP 200; the hub is
idempotent (UNIQUE(node,src_id)) so optimistic advancement is safe.

Default: drain once and exit (for a timer). Set SMOKEMON_SHIP_INTERVAL>0 to loop."""

import json
import sqlite3
import sys
import urllib.error
import urllib.request

from . import config, core, schema


def init_state(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS ship_state "
                 "(table_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0)")
    conn.commit()


def _last(conn, table: str) -> int:
    row = conn.execute("SELECT last_id FROM ship_state WHERE table_name=?", (table,)).fetchone()
    return row[0] if row else 0


def _set_last(conn, table: str, value: int) -> None:
    conn.execute("INSERT INTO ship_state (table_name,last_id) VALUES (?,?) "
                 "ON CONFLICT(table_name) DO UPDATE SET last_id=excluded.last_id", (table, value))


def gather(conn) -> tuple[dict, dict]:
    payload: dict[str, dict] = {}
    maxids: dict[str, int] = {}
    for t in schema.STD_TABLES:
        last = _last(conn, t)
        try:
            cur = conn.execute(f"SELECT * FROM {t} WHERE id>? ORDER BY id LIMIT ?", (last, config.SHIP_BATCH))
        except sqlite3.OperationalError:
            continue  # table not present on this node yet
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if rows:
            payload[t] = {"columns": cols, "rows": [list(r) for r in rows]}
            maxids[t] = rows[-1][cols.index("id")]
    # ping_rtts ships keyed to already-shipped ping_runs (run_id <= their max).
    runs_cap = maxids.get("ping_runs", _last(conn, "ping_runs"))
    rtt_last = _last(conn, "ping_rtts")
    if runs_cap > rtt_last:
        try:
            rrows = conn.execute("SELECT run_id, rtt_ms FROM ping_rtts WHERE run_id>? AND run_id<=? ORDER BY run_id",
                                 (rtt_last, runs_cap)).fetchall()
            if rrows:
                payload["ping_rtts"] = {"columns": ["run_id", "rtt_ms"], "rows": [list(r) for r in rrows]}
            maxids["ping_rtts"] = runs_cap
        except sqlite3.OperationalError:
            pass
    return payload, maxids


def _post(payload: dict) -> bool:
    req = urllib.request.Request(
        config.HUB_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Smokemon-Key": config.HUB_SECRET})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        core.log(f"POST failed: {e!r}")
        return False


def drain(conn) -> int:
    total = 0
    while not core.stopping():
        payload, maxids = gather(conn)
        if not payload:
            if "ping_rtts" in maxids:  # only cursor advance, nothing to send
                _set_last(conn, "ping_rtts", maxids["ping_rtts"])
                conn.commit()
            break
        if not _post({"node": config.NODE, "tables": payload}):
            break  # retry next run; cursor unchanged
        for t, mid in maxids.items():
            _set_last(conn, t, mid)
        conn.commit()
        total += sum(len(t["rows"]) for t in payload.values())
        # ping_rtts is capped to already-shipped runs, not SHIP_BATCH, so exclude it:
        # if no std table filled a full batch, the backlog is drained and we can stop.
        if not any(len(v["rows"]) >= config.SHIP_BATCH for k, v in payload.items() if k != "ping_rtts"):
            break
    return total


def main() -> int:
    if not config.HUB_URL:
        core.log("ship: SMOKEMON_HUB_URL not set, nothing to do")
        return 0
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    init_state(conn)

    def once() -> None:
        n = drain(conn)
        if n:
            core.log(f"shipped {n} rows")

    if config.SHIP_INTERVAL > 0:
        core.log(f"ship daemon: node={config.NODE} hub={config.HUB_URL} interval={config.SHIP_INTERVAL}s")
        core.run_scheduler([(config.SHIP_INTERVAL, once)])  # runs now, then every interval
    else:
        core.log(f"ship once: node={config.NODE} hub={config.HUB_URL} shipped {drain(conn)} rows")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
