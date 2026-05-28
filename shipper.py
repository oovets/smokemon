#!/usr/bin/env python3
"""smokemon shipper — skickar nya rader (delta per stigande id) från nodens lokala
SQLite till hubbens /ingest. En lokal tabell ship_state(table_name,last_id) håller
markören per tabell; markören avanceras bara vid HTTP 200. Hubben är idempotent
(UNIQUE(node, src_id)) så optimistisk avancering är säker. Ren stdlib.

Default: töm kön och avsluta (lämplig för en systemd-timer/launchd StartInterval).
Sätt SMOKEMON_SHIP_INTERVAL>0 för daemon-läge (loopar)."""

import json
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request

import platform_adapters as pa

NODE = pa.NODE
HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")
DB_PATH = os.environ.get("SMOKEMON_DB", DEFAULT_DB)
HUB_URL = os.environ.get("SMOKEMON_HUB_URL", "http://100.87.219.2:8765/ingest")
SECRET = os.environ.get("SMOKEMON_HUB_SECRET", "changeme")
BATCH = int(os.environ.get("SMOKEMON_SHIP_BATCH", "2000"))
INTERVAL = float(os.environ.get("SMOKEMON_SHIP_INTERVAL", "0"))  # 0 = en gång, töm och avsluta

# Append-only tabeller med stigande 'id' som skeppas generiskt.
STD_TABLES = (
    "ping_runs", "net_samples", "http_samples", "mtr_hops", "wifi_samples",
    "iperf_samples", "host_samples", "disk_samples", "proc_samples",
)

_running = True


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _stop(signum, _frame):
    global _running
    _running = False


def init_state(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ship_state (table_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()


def get_last(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute("SELECT last_id FROM ship_state WHERE table_name=?", (table,)).fetchone()
    return row[0] if row else 0


def set_last(conn: sqlite3.Connection, table: str, value: int) -> None:
    conn.execute(
        "INSERT INTO ship_state (table_name,last_id) VALUES (?,?) "
        "ON CONFLICT(table_name) DO UPDATE SET last_id=excluded.last_id",
        (table, value),
    )


def gather(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Plocka nästa batch per tabell. Returnerar (payload_tables, maxids)."""
    payload: dict[str, dict] = {}
    maxids: dict[str, int] = {}
    for t in STD_TABLES:
        last = get_last(conn, t)
        try:
            cur = conn.execute(f"SELECT * FROM {t} WHERE id>? ORDER BY id LIMIT ?", (last, BATCH))
        except sqlite3.OperationalError:
            continue  # tabellen finns inte än på den här noden
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if rows:
            payload[t] = {"columns": cols, "rows": [list(r) for r in rows]}
            maxids[t] = rows[-1][cols.index("id")]
    # ping_rtts: skeppas kopplat till redan skeppade ping_runs (run_id <= deras maxid).
    runs_cap = maxids.get("ping_runs", get_last(conn, "ping_runs"))
    rtt_last = get_last(conn, "ping_rtts")
    if runs_cap > rtt_last:
        try:
            rrows = conn.execute(
                "SELECT run_id, rtt_ms FROM ping_rtts WHERE run_id>? AND run_id<=? ORDER BY run_id",
                (rtt_last, runs_cap),
            ).fetchall()
            if rrows:
                payload["ping_rtts"] = {"columns": ["run_id", "rtt_ms"], "rows": [list(r) for r in rrows]}
            maxids["ping_rtts"] = runs_cap
        except sqlite3.OperationalError:
            pass
    return payload, maxids


def post(payload: dict) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HUB_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Smokemon-Key": SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        log(f"POST misslyckades: {e!r}")
        return False


def drain(conn: sqlite3.Connection) -> int:
    total = 0
    while _running:
        payload, maxids = gather(conn)
        nrows = sum(len(t["rows"]) for t in payload.values())
        if not payload:
            if "ping_rtts" in maxids:  # bara markör-avancering, inget att skicka
                set_last(conn, "ping_rtts", maxids["ping_rtts"])
                conn.commit()
            break
        if not post({"node": NODE, "tables": payload}):
            break  # försök igen vid nästa körning; markören står kvar
        for t, mid in maxids.items():
            set_last(conn, t, mid)
        conn.commit()
        total += nrows
        # Om någon tabell fyllde hela batchen finns troligen mer -> fortsätt tömma.
        more = any(len(payload[t]["rows"]) >= BATCH for t in STD_TABLES if t in payload)
        if not more:
            break
    return total


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_state(conn)
    if INTERVAL > 0:
        log(f"start daemon: node={NODE} hub={HUB_URL} interval={INTERVAL}s")
        while _running:
            n = drain(conn)
            if n:
                log(f"skeppade {n} rader")
            slept = 0.0
            while slept < INTERVAL and _running:
                time.sleep(min(1.0, INTERVAL - slept))
                slept += 1.0
    else:
        n = drain(conn)
        log(f"node={NODE} hub={HUB_URL}: skeppade {n} rader")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
