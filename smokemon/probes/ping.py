"""Latency + packet loss via fping (one ping_runs row + every individual ping_rtts).
Pre-aggregates p25/p75 (in addition to min/median/max already kept) so the percentile
renderer can read ping_runs only and skip the full ping_rtts scan for new data."""

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


def _stats(rtts: list[float]) -> tuple[float | None, ...]:
    """(min, p25, median, p75, mean, max, stddev) or all-None if empty.
    quantiles(n=4) returns the three cut points [p25, p50, p75]; needs >= 2 samples."""
    if not rtts:
        return (None, None, None, None, None, None, None)
    mn, mx = min(rtts), max(rtts)
    if len(rtts) >= 2:
        p25, p50, p75 = statistics.quantiles(rtts, n=4)
    else:
        p25 = p50 = p75 = rtts[0]
    return (mn, p25, p50, p75, statistics.fmean(rtts), mx,
            statistics.pstdev(rtts) if len(rtts) > 1 else 0.0)


def _build_run(ts: float, target: str, samples: list[float | None]) -> tuple[dict, list[float]]:
    rtts = [s for s in samples if s is not None]
    sent, recv = len(samples), len(rtts)
    mn, p25, p50, p75, mean, mx, sd = _stats(rtts)
    run = dict(zip(
        ("ts", "target", "sent", "recv", "loss_pct",
         "rtt_min", "rtt_p25", "rtt_median", "rtt_p75", "rtt_avg", "rtt_max", "rtt_stddev"),
        (ts, target, sent, recv, 100.0 * (sent - recv) / sent if sent else 0.0,
         mn, p25, p50, p75, mean, mx, sd)))
    return run, rtts


def collect(conn) -> None:
    ts = time.time()
    results = _run_fping()
    all_rtts: list[tuple[int, float]] = []
    for target in config.TARGETS:
        run, rtts = _build_run(ts, target, results.get(target, []))
        run_id = schema.insert_one(conn, "ping_runs", run)
        all_rtts.extend((run_id, r) for r in rtts)
    if all_rtts:
        conn.executemany("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)", all_rtts)
    conn.commit()
