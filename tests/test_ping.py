"""ping._stats / _build_run: the write-side producer of the pre-aggregated percentiles
that load_ping_smoke reads back. Covers the empty, single-sample, and multi-sample
branches plus loss_pct math."""

import statistics

from smokemon.probes import ping


def test_stats_empty_returns_all_none():
    assert ping._stats([]) == (None, None, None, None, None, None, None)


def test_stats_single_sample_collapses_percentiles():
    # len < 2: quantiles() can't run, so p25/p50/p75 all fall back to the lone value
    # and stddev is 0.0 (not a raise).
    mn, p25, p50, p75, mean, mx, sd = ping._stats([5.0])
    assert mn == 5.0 and mx == 5.0 and mean == 5.0
    assert p25 == 5.0 and p50 == 5.0 and p75 == 5.0
    assert sd == 0.0


def test_stats_multi_sample_matches_stdlib():
    rtts = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    mn, p25, p50, p75, mean, mx, sd = ping._stats(rtts)
    exp_p25, exp_p50, exp_p75 = statistics.quantiles(rtts, n=4)
    assert mn == 5.0 and mx == 10.0
    assert mean == statistics.fmean(rtts)
    assert (p25, p50, p75) == (exp_p25, exp_p50, exp_p75)
    assert sd == statistics.pstdev(rtts)


def test_build_run_loss_pct_and_columns():
    # 5 sent, 2 lost (None) -> 3 received -> 40% loss; rtts list excludes the None holes.
    samples = [10.0, None, 12.0, None, 11.0]
    run, rtts = ping._build_run(1000.0, "1.1.1.1", samples)
    assert run["ts"] == 1000.0 and run["target"] == "1.1.1.1"
    assert run["sent"] == 5 and run["recv"] == 3
    assert run["loss_pct"] == 40.0
    assert rtts == [10.0, 12.0, 11.0]
    assert run["rtt_p25"] is not None and run["rtt_p75"] is not None


def test_build_run_all_lost():
    run, rtts = ping._build_run(1000.0, "gw", [None, None])
    assert run["sent"] == 2 and run["recv"] == 0
    assert run["loss_pct"] == 100.0
    assert rtts == []
    assert run["rtt_median"] is None


def test_build_run_no_result_line_is_total_loss():
    """fping emits no result line at all for a target it cannot resolve, so _build_run gets
    an empty sample list. That used to yield loss_pct=0.0, which made a target that stopped
    resolving render as permanently healthy -- the exact failure the tool exists to catch.
    It must read as total loss."""
    run, rtts = ping._build_run(1000.0, "nosuchhost.invalid", [])
    assert run["sent"] == 0 and run["recv"] == 0
    assert run["loss_pct"] == 100.0
    assert rtts == []
    assert run["rtt_median"] is None


def test_parse_rtts_handles_mixed_tokens():
    assert ping._parse_rtts("10.0 - 12.5") == [10.0, None, 12.5]
    assert ping._parse_rtts("") == []


def test_parse_rtts_rejects_diagnostic_line():
    """fping writes diagnostics to the same stream as results ("<host>: Name or service not
    known"). Calling float() on those tokens used to raise and kill the probe for *every*
    target, not just the broken one."""
    assert ping._parse_rtts("Name or service not known") == []
    assert ping._parse_rtts("10.0 bogus 12.0") == []
