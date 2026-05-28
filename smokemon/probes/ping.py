"""Latency + packet loss via fping (one ping_runs row + every individual ping_rtts)."""

import statistics
import subprocess
import time

from .. import config, schema


def _run_fping() -> dict[str, list[float | None]]:
    cmd = [config.FPING, "-C", str(config.PING_COUNT), "-p", str(config.PING_PERIOD), "-q", *config.TARGETS]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=config.PING_COUNT * config.PING_PERIOD / 1000 + 30)
    out = proc.stderr or proc.stdout  # fping writes per-target results to stderr in -q -C mode
    results: dict[str, list[float | None]] = {}
    for line in out.splitlines():
        target, sep, rest = line.partition(":")
        target = target.strip()
        if sep and target in config.TARGETS:
            results[target] = [None if tok == "-" else float(tok) for tok in rest.split()]
    return results


def _store(conn, ts: float, target: str, samples: list[float | None]) -> None:
    rtts = [s for s in samples if s is not None]
    sent, recv = len(samples), len(rtts)
    stats = ((min(rtts), statistics.median(rtts), statistics.fmean(rtts), max(rtts), statistics.pstdev(rtts))
             if rtts else (None, None, None, None, None))
    run = dict(zip(
        ("ts", "target", "sent", "recv", "loss_pct", "rtt_min", "rtt_median", "rtt_avg", "rtt_max", "rtt_stddev"),
        (ts, target, sent, recv, 100.0 * (sent - recv) / sent if sent else 0.0, *stats)))
    run_id = schema.insert_one(conn, "ping_runs", run)
    if rtts:
        conn.executemany("INSERT INTO ping_rtts (run_id,rtt_ms) VALUES (?,?)", [(run_id, r) for r in rtts])


def collect(conn) -> None:
    ts = time.time()
    results = _run_fping()
    for target in config.TARGETS:
        _store(conn, ts, target, results.get(target, []))
    conn.commit()
