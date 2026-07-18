#!/usr/bin/env python3
"""Measure what a healthy node actually writes to disk.

The whole justification for incident-centric storage is a write budget, and the numbers in the
README were estimates. This measures them instead.

Two measurements are taken:

  * **Physical bytes**, from /proc/self/io `write_bytes` -- the kernel's count of bytes sent to
    the storage layer, which is what wears an SD card. This is ground truth, and it is only
    available on Linux, which is where the fleet runs.
  * **Appended bytes**, the summed positive growth of the database and its WAL. Portable, and a
    good proxy in WAL mode because the WAL is append-only. Reported everywhere so the script is
    still useful on a dev machine.

The gap between the two is write amplification: SQLite commits whole pages and appends a commit
frame per transaction, so a 180-byte heartbeat row does not cost 180 bytes.

    python3 scripts/bench_write_budget.py --hours 24
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smokemon import baseline, core, detect, heartbeat, incidents, schema, signals  # noqa: E402


def _proc_write_bytes() -> int | None:
    """Cumulative bytes this process has sent to the storage layer. Linux only."""
    try:
        with open("/proc/self/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


class Meter:
    def __init__(self, path: str) -> None:
        self.path = path
        self.appended = 0
        self._prev = self._sizes()
        self._io0 = _proc_write_bytes()
        self.commits = 0

    def _sizes(self) -> tuple[int, int]:
        def size(p: str) -> int:
            try:
                return os.path.getsize(p)
            except OSError:
                return 0
        return (size(self.path), size(self.path + "-wal"))

    def sample(self) -> None:
        """Call after each commit. Counts growth only: a WAL checkpoint truncates the file,
        and that shrink is not a negative write."""
        db, wal = self._sizes()
        pdb, pwal = self._prev
        self.appended += max(0, db - pdb) + max(0, wal - pwal)
        self._prev = (db, wal)
        self.commits += 1

    def physical(self) -> int | None:
        io1 = _proc_write_bytes()
        return None if (self._io0 is None or io1 is None) else io1 - self._io0


def run(hours: float, ping_interval: float, host_interval: float,
        hb_interval: float, incidents_per_day: float) -> dict:
    """Simulate a node's steady state and measure what reaches disk.

    Samples are fed through the real detector and the real persistence path, so this measures
    the shipping code rather than a model of it. Incidents are injected at a configurable rate
    because they are the variable part of the budget -- the heartbeat is the fixed part."""
    signals.reset(); baseline.reset(); detect.reset()
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "node.db")
    try:
        conn = core.connect(path)
        schema.init_node(conn)
        incidents.ensure_table(conn)
        baseline.ensure_table(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()

        m = Meter(path)
        t0 = time.time()
        total_s = hours * 3600
        # Deterministic incident placement: no RNG, so two runs of the same config are
        # comparable and a regression in the write budget is unambiguous.
        n_incidents = max(0, int(incidents_per_day * hours / 24))
        every = total_s / (n_incidents + 1)
        wall = mono = 0.0
        next_hb = hb_interval
        next_host = host_interval
        next_inc = every
        bad_until = -1.0

        while wall < total_s:
            wall += ping_interval
            mono += ping_interval
            if n_incidents and wall >= next_inc:
                bad_until = wall + 180.0        # a three-minute incident
                next_inc += every
            loss = 55.0 if wall < bad_until else 0.0
            acts = detect.evaluate("ping.loss", "1.1.1.1", loss, t0 + wall, mono)
            if incidents.apply(conn, acts):
                m.sample()
            if wall >= next_host:
                next_host += host_interval
                for sig, val in (("host.temp", 48.0), ("host.mem", 41.0),
                                 ("host.swap", 0.0)):
                    if incidents.apply(conn, detect.evaluate(sig, "", val, t0 + wall, mono)):
                        m.sample()
            if wall >= next_hb:
                next_hb += hb_interval
                heartbeat.collect(conn)
                m.sample()

        baseline.maybe_flush(conn, t0 + wall, force=True)
        m.sample()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        m.sample()

        rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in schema.STD_TABLES}
        final = os.path.getsize(path)
        conn.close()
        return {"hours": hours, "commits": m.commits, "appended": m.appended,
                "physical": m.physical(), "db_bytes": final, "rows": rows,
                "incidents": n_incidents}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _mb(n) -> str:
    return "n/a" if n is None else f"{n / 1e6:.2f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--incidents-per-day", type=float, default=2.0)
    ap.add_argument("--heartbeat", type=float, default=300.0)
    args = ap.parse_args()

    r = run(args.hours, ping_interval=10.0, host_interval=30.0,
            hb_interval=args.heartbeat, incidents_per_day=args.incidents_per_day)
    per_day = 24.0 / r["hours"]

    print(f"simulated {r['hours']:g} h at 10 s ping / 30 s host / {args.heartbeat:g} s heartbeat,"
          f" {r['incidents']} incident(s)\n")
    print(f"  commits            {r['commits']:>10,}   ({r['commits'] * per_day:,.0f}/day)")
    print(f"  appended (db+wal)  {_mb(r['appended']):>10}   ({_mb(r['appended'] * per_day)}/day)")
    print(f"  physical writes    {_mb(r['physical']):>10}   "
          f"({_mb(r['physical'] * per_day) if r['physical'] is not None else 'n/a'}/day)")
    if r["physical"] is None:
        print("      (physical writes need /proc/self/io -- run this on the Linux node to get"
              " the number that matters for card wear)")
    print(f"  final db size      {_mb(r['db_bytes']):>10}")
    print("\n  rows written:")
    for t, n in sorted(r["rows"].items(), key=lambda kv: -kv[1]):
        if n:
            print(f"    {t:<20} {n:>8,}   ({n * per_day:,.0f}/day)")
    idle = [t for t, n in r["rows"].items() if not n]
    if idle:
        print(f"    untouched: {', '.join(sorted(idle))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
