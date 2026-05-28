#!/usr/bin/env python3
"""smokemon collector: sample latency/loss (fping) and per-interface bandwidth
(netstat byte counters) at a fixed cadence into SQLite. Stdlib only."""

import os
import signal
import sqlite3
import statistics
import subprocess
import sys
import time

import platform_adapters as pa

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")

TARGETS = [t.strip() for t in os.environ.get("SMOKEMON_TARGETS", "1.1.1.1,192.168.0.1").split(",") if t.strip()]
INTERVAL = float(os.environ.get("SMOKEMON_INTERVAL", "10"))
COUNT = int(os.environ.get("SMOKEMON_COUNT", "20"))   # pings per cycle per target
PERIOD = int(os.environ.get("SMOKEMON_PERIOD", "50"))  # ms between pings
FPING = pa.cli_path("SMOKEMON_FPING", "fping")
DB_PATH = os.environ.get("SMOKEMON_DB", DEFAULT_DB)
NODE = pa.NODE

_running = True


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _stop(signum, _frame):
    global _running
    _running = False
    log(f"signal {signum} received, exiting after current cycle")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ping_runs (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            target TEXT NOT NULL,
            sent INTEGER NOT NULL,
            recv INTEGER NOT NULL,
            loss_pct REAL NOT NULL,
            rtt_min REAL, rtt_median REAL, rtt_avg REAL, rtt_max REAL, rtt_stddev REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ping_runs_ts ON ping_runs(ts);
        CREATE INDEX IF NOT EXISTS ix_ping_runs_target_ts ON ping_runs(target, ts);

        CREATE TABLE IF NOT EXISTS ping_rtts (
            run_id INTEGER NOT NULL,
            rtt_ms REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ping_rtts_run ON ping_rtts(run_id);

        CREATE TABLE IF NOT EXISTS net_samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            iface TEXT NOT NULL,
            ibytes INTEGER NOT NULL,
            obytes INTEGER NOT NULL,
            ipkts INTEGER NOT NULL,
            opkts INTEGER NOT NULL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_net_ts ON net_samples(ts);
        CREATE INDEX IF NOT EXISTS ix_net_iface_ts ON net_samples(iface, ts);
        """
    )
    conn.commit()
    pa.ensure_node_column(conn, ("ping_runs", "net_samples"))


def run_fping() -> dict[str, list[float | None]]:
    """Return {target: [rtt_ms or None per probe]}."""
    cmd = [FPING, "-C", str(COUNT), "-p", str(PERIOD), "-q", *TARGETS]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=COUNT * PERIOD / 1000 + 30)
    out = proc.stderr or proc.stdout  # fping writes per-target results to stderr in -q -C mode
    results: dict[str, list[float | None]] = {}
    for line in out.splitlines():
        target, sep, rest = line.partition(":")
        target = target.strip()
        if not sep or target not in TARGETS:
            continue
        results[target] = [None if tok == "-" else float(tok) for tok in rest.split()]
    return results


def store_ping(conn: sqlite3.Connection, ts: float, target: str, samples: list[float | None]) -> None:
    rtts = [s for s in samples if s is not None]
    sent, recv = len(samples), len(rtts)
    loss_pct = 100.0 * (sent - recv) / sent if sent else 0.0
    stats = (
        (min(rtts), statistics.median(rtts), statistics.fmean(rtts), max(rtts), statistics.pstdev(rtts))
        if rtts else (None, None, None, None, None)
    )
    cur = conn.execute(
        "INSERT INTO ping_runs (ts,target,sent,recv,loss_pct,rtt_min,rtt_median,rtt_avg,rtt_max,rtt_stddev,node)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, target, sent, recv, loss_pct, *stats, NODE),
    )
    if rtts:
        conn.executemany("INSERT INTO ping_rtts (run_id,rtt_ms) VALUES (?,?)", [(cur.lastrowid, r) for r in rtts])


def store_net(conn: sqlite3.Connection, ts: float, rows: list[tuple[str, int, int, int, int]]) -> None:
    conn.executemany(
        "INSERT INTO net_samples (ts,iface,ibytes,obytes,ipkts,opkts,node) VALUES (?,?,?,?,?,?,?)",
        [(ts, *row, NODE) for row in rows],
    )


def cycle(conn: sqlite3.Connection) -> None:
    ts = time.time()
    try:
        results = run_fping()
        for target in TARGETS:
            store_ping(conn, ts, target, results.get(target, []))
    except Exception as e:  # noqa: BLE001 - one bad cycle must never kill the daemon
        log(f"fping error: {e!r}")
    try:
        store_net(conn, ts, pa.read_net_counters())
    except Exception as e:  # noqa: BLE001
        log(f"net-counter error: {e!r}")
    conn.commit()


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)
    log(f"start: node={NODE} targets={TARGETS} interval={INTERVAL}s count={COUNT} period={PERIOD}ms db={DB_PATH}")
    while _running:
        start = time.time()
        cycle(conn)
        next_t = (int(start // INTERVAL) + 1) * INTERVAL  # align to wall-clock multiples of INTERVAL
        sleep = next_t - time.time()
        while sleep > 0 and _running:
            time.sleep(min(sleep, 1.0))
            sleep = next_t - time.time()
    conn.close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
