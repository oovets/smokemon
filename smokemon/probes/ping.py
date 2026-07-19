"""Latency + packet loss via fping, fed to the detector.

Runs one fping cycle per interval, aggregates the raw RTTs into per-target min/p25/median/
p75/mean/max/stddev plus a loss percentage, canonicalises the entity (the target AS
CONFIGURED, not a resolved address), and hands the values to incidents.evaluate(). Nothing is
written per cycle: the detector keeps samples in memory and persists only what a rule
confirms, so the individual RTTs live and die inside this call."""

import statistics
import subprocess
import time

from .. import config, incidents


def _run_fping() -> dict[str, list[float | None]]:
    cmd = [config.FPING, "-C", str(config.PING_COUNT), "-p", str(config.PING_PERIOD), "-q", *config.TARGETS]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=config.PING_COUNT * config.PING_PERIOD / 1000 + 30)
    out = proc.stderr or proc.stdout  # fping writes per-target results to stderr in -q -C mode
    results: dict[str, list[float | None]] = {}
    for line in out.splitlines():
        target, sep, rest = line.partition(":")
        target = target.strip()
        if not sep or target not in config.TARGETS:
            continue
        got = _parse_rtts(rest)
        # Only a parsed result line counts. fping emits other lines under the same
        # "<target> : ..." prefix -- most commonly
        #     1.1.1.1 : duplicate for [0], 64 bytes, 12.1 ms
        # when a duplicate ICMP reply arrives. Assigning the empty parse from one of those
        # would discard the real result that already came in, and since a run with no samples
        # is reported as total loss, a healthy link would read as a complete outage. Two such
        # cycles are enough to open a crit incident for an outage that never happened.
        if got:
            results[target] = got
    return results


def _parse_rtts(rest: str) -> list[float | None]:
    """Tokens of an fping -C result line, or [] if this is not one.

    A result line is entirely "-" and numbers. Anything else is a diagnostic ("Name or service
    not known", "duplicate for [0], 64 bytes, 12.1 ms") and is rejected whole rather than
    raising: one unresolvable or chatty target must not kill the probe for every other."""
    out: list[float | None] = []
    for tok in rest.split():
        if tok == "-":
            out.append(None)
            continue
        try:
            out.append(float(tok))
        except ValueError:
            return []
    return out


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
        # sent == 0 means fping produced no result line for this target at all (unresolvable,
        # or it failed outright). That is total failure to reach it, not a clean run -- reporting
        # 0.0 here made a target that stopped resolving read as permanently healthy.
        (ts, target, sent, recv, 100.0 * (sent - recv) / sent if sent else 100.0,
         mn, p25, p50, p75, mean, mx, sd)))
    return run, rtts


def collect(conn) -> None:
    ts = time.time()
    results = _run_fping()
    runs: list[tuple[str, dict]] = []
    for target in config.TARGETS:
        run, _rtts = _build_run(ts, target, results.get(target, []))
        runs.append((target, run))

    # Feed the detector off values already computed -- no extra probing. loss is fed as the
    # run aggregate (sent/recv over the whole fping cycle), never a single ping, which would
    # only ever be 0 or 100 and make every threshold between them meaningless.
    # Entity is the target AS CONFIGURED, not a resolved address: a name that resolves
    # somewhere else tomorrow must stay the same signal rather than becoming a second one
    # with a cold baseline.
    for target, run in runs:
        loss = run["loss_pct"]
        incidents.evaluate(conn, "ping.loss", target, loss, ts)
        incidents.evaluate(conn, "ping.loss_run", target, loss, ts)
        incidents.evaluate(conn, "ping.rtt_med", target, run["rtt_median"], ts)
