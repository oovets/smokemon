"""Benchmark the hub's heavy read endpoints against a realistic hub DB.

Two modes:
  - synthetic (default): build a temp hub DB with --nodes nodes x --days days of ping_runs +
    host_samples at the real collector cadence, then time the endpoints.
  - real: point at an existing hub DB with --db PATH (read-only); nothing is written to it.

For each endpoint it reports wall time, and the raw-vs-rollup delta on the per-node loader that
dominates fleet()/risks(), so you can see where the seconds actually go and what downsampling
buys (long windows read hub rollups; short windows read raw).

Run:  python -m scripts.bench_hub_reads --nodes 20 --days 14
      python -m scripts.bench_hub_reads --db /path/to/smokemon-hub.db
"""

import argparse
import os
import sys
import tempfile
import time

# allow running as `python scripts/bench_hub_reads.py` from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smokemon import core, hubapi, query, rollup, schema  # noqa: E402


def _seed(conn, nodes: int, days: int, until: float) -> int:
    """Seed `nodes` nodes with `days` of ping_runs (10s) + host_samples (30s). Returns rows."""
    since = until - days * 86400
    total = 0
    for n in range(nodes):
        node = f"node{n:03d}"
        ping, host = [], []
        # 10s ping cadence is the real fast loop; batch-insert per node to keep memory bounded.
        t = since
        while t < until:
            loss = 0.0 if (int(t) % 997) else 100.0  # an occasional outage so incidents exist
            med = 8.0 + (int(t) % 13)
            ping.append({"ts": t, "target": "1.1.1.1", "sent": 20, "recv": 20, "loss_pct": loss,
                         "rtt_min": 5.0, "rtt_p25": 6.0, "rtt_median": med, "rtt_p75": med + 2,
                         "rtt_avg": med, "rtt_max": med + 5, "rtt_stddev": 1.5})
            if int(t) % 30 == 0:
                host.append({"ts": t, "cpu_pct": 20.0 + (int(t) % 40), "mem_used_pct": 40.0,
                             "temp_c": 50.0 + (int(t) % 10)})
            t += 10
            if len(ping) >= 5000:
                schema.insert(conn, "ping_runs", ping, node=node)
                total += len(ping); ping = []
        if ping:
            schema.insert(conn, "ping_runs", ping, node=node); total += len(ping)
        if host:
            schema.insert(conn, "host_samples", host, node=node); total += len(host)
        conn.commit()
    return total


def _time(label: str, fn, repeat: int = 3) -> float:
    """Best-of-`repeat` wall time in ms (best run = least noise)."""
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    ms = best * 1000.0
    print(f"  {label:<34} {ms:8.1f} ms")
    return ms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="existing hub DB (read-only); skips synthetic seeding")
    ap.add_argument("--nodes", type=int, default=20)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--hours", type=float, default=24.0, help="query window for the endpoints")
    args = ap.parse_args()

    if args.db:
        db = args.db
        conn = query.open_ro(db)
        print(f"benchmarking existing hub DB: {db} (read-only)")
    else:
        db = tempfile.mktemp(suffix="-hub.db")
        os.environ["SMOKEMON_HUB_DB"] = db
        wconn = core.connect(db)
        schema.init_hub(wconn)
        until = time.time()
        print(f"seeding synthetic hub: {args.nodes} nodes x {args.days} days (10s ping) -> {db}")
        t0 = time.perf_counter()
        rows = _seed(wconn, args.nodes, args.days, until)
        print(f"  seeded {rows:,} rows in {time.perf_counter() - t0:.1f}s")
        print("  building rollups...")
        t0 = time.perf_counter()
        written = rollup.rollup(wconn, now=until)
        print(f"  rollup wrote {sum(written.values()):,} buckets in {time.perf_counter() - t0:.1f}s")
        wconn.close()
        conn = query.open_ro(db)

    raw_count = conn.execute("SELECT COUNT(*) FROM ping_runs").fetchone()[0]
    now = time.time()
    since = now - args.hours * 3600
    res = query._resolution(since, now)
    print(f"\nraw ping_runs rows: {raw_count:,} | window: {args.hours}h | auto-res = '{res or 'raw'}'\n")

    print("endpoint timings (best of 3) - these now read rollups for long windows:")
    _time("fleet()", lambda: hubapi.fleet(conn, args.hours))
    _time("risks()", lambda: hubapi.risks(conn, args.hours))
    _time("heatmap(loss)", lambda: hubapi.heatmap(conn, "loss", args.hours))

    # Show the raw-vs-rollup delta on the loader that dominates fleet()/risks().
    nodes = hubapi.nodes(conn)

    def _all(res_):
        for nd in nodes:
            query.load_ping_agg(conn, since, now, None, nd, res=res_)

    print(f"\nload_ping_agg x{len(nodes)} nodes, raw vs rollup:")
    _time("load_ping_agg [raw]", lambda: _all(""))
    if res:
        _time(f"load_ping_agg [{res}]", lambda: _all(res))

    conn.close()
    if not args.db:
        os.unlink(db)
        for ext in ("-wal", "-shm"):
            try:
                os.unlink(db + ext)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
