#!/usr/bin/env python3
"""smokemon iperf3 probe: active throughput test (up + down) to a Tailscale peer.
Run sparsely by launchd (it consumes real bandwidth). Requires 'iperf3 -s' on the
server. Stdlib only."""

import json
import os
import sqlite3
import subprocess
import sys
import time

import platform_adapters as pa

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")

DB_PATH = os.environ.get("SMOKEMON_DB", DEFAULT_DB)
SERVER = os.environ.get("SMOKEMON_IPERF_SERVER", "100.87.219.2")
DURATION = os.environ.get("SMOKEMON_IPERF_DURATION", "5")
IPERF = pa.cli_path("SMOKEMON_IPERF", "iperf3")
NODE = pa.NODE


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def run_iperf(reverse: bool) -> dict | None:
    cmd = [IPERF, "-c", SERVER, "-J", "-t", DURATION, "--connect-timeout", "5000"]
    if reverse:
        cmd.append("-R")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(DURATION) + 30)
    except Exception as e:  # noqa: BLE001
        log(f"iperf3 error (reverse={reverse}): {e!r}")
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        log(f"iperf3 invalid JSON (reverse={reverse}): {proc.stderr[:160]}")
        return None
    if "error" in data:
        log(f"iperf3 server error: {data['error']}")
        return None
    return data


def main() -> int:
    up = run_iperf(reverse=False)
    down = run_iperf(reverse=True)
    if not up and not down:
        log(f"no result - is 'iperf3 -s' running on {SERVER}?")
        return 1

    ts = time.time()
    up_mbps = up["end"]["sum_sent"]["bits_per_second"] / 1e6 if up else None
    retrans = up["end"]["sum_sent"].get("retransmits") if up else None
    down_mbps = down["end"]["sum_received"]["bits_per_second"] / 1e6 if down else None

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS iperf_samples (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, server TEXT NOT NULL,
            up_mbps REAL, down_mbps REAL, retransmits INTEGER, node TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_iperf_ts ON iperf_samples(ts)")
    pa.ensure_node_column(conn, ("iperf_samples",))
    conn.execute(
        "INSERT INTO iperf_samples (ts,server,up_mbps,down_mbps,retransmits,node) VALUES (?,?,?,?,?,?)",
        (ts, SERVER, up_mbps, down_mbps, retrans, NODE),
    )
    conn.commit()
    conn.close()
    log(f"saved: {SERVER} up={up_mbps and round(up_mbps, 1)} Mbit/s "
        f"down={down_mbps and round(down_mbps, 1)} Mbit/s retrans={retrans}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
