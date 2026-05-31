"""Multivariate anomaly detection - smokemon.mlanomaly.

The stdlib fallback path is exercised by forcing _HAS_NUMPY off; the numpy path is tested
under importorskip so the suite still runs on a bare (no-extras) checkout. The function only
reads frame['t'] and frame['series'], so the tests build a frame dict directly rather than
seeding a DB."""

import pytest

from smokemon import mlanomaly


def _frame(series: dict) -> dict:
    """A minimal analysis frame: a 12-bucket grid plus the given signal columns."""
    n = len(next(iter(series.values())))
    return {"t": [float(i * 60) for i in range(n)], "bucket": 60.0, "series": series}


def _co_deviation_frame():
    """Three signals quiet for 11 buckets, then all three nudge up together in the last
    bucket - each only a moderate single-signal deviation, but jointly anomalous."""
    quiet = [10.0] * 11
    cpu = quiet + [22.0]
    temp = [50.0] * 11 + [60.0]
    rtt = [20.0] * 11 + [40.0]
    return _frame({"cpu": cpu, "temp": temp, "rtt": rtt})


def test_stdlib_path_flags_co_deviation(monkeypatch):
    monkeypatch.setattr(mlanomaly, "_HAS_NUMPY", False)
    res = mlanomaly.multivariate_anomalies(_co_deviation_frame(), z_floor=2.0, co_min=2,
                                           score_thresh=3.0)
    assert res, "co-deviating bucket should be flagged"
    top = res[0]
    assert top["ts"] == 11 * 60.0  # the last bucket
    sig_names = {name for name, _z in top["signals"]}
    assert {"cpu", "temp", "rtt"} <= sig_names  # all three named -> explainable
    assert len(top["signals"]) >= 2


def test_stdlib_path_quiet_frame_no_anomaly(monkeypatch):
    monkeypatch.setattr(mlanomaly, "_HAS_NUMPY", False)
    flat = _frame({"cpu": [10.0] * 12, "temp": [50.0] * 12, "rtt": [20.0] * 12})
    assert mlanomaly.multivariate_anomalies(flat) == []


def test_single_signal_below_co_min(monkeypatch):
    """Only one signal deviating must not trip the multivariate detector (that is what the
    univariate tod_anomalies is for); co_min defaults to 2."""
    monkeypatch.setattr(mlanomaly, "_HAS_NUMPY", False)
    f = _frame({"cpu": [10.0] * 11 + [90.0], "temp": [50.0] * 12})
    assert mlanomaly.multivariate_anomalies(f, co_min=2) == []


def test_too_few_buckets_or_signals(monkeypatch):
    monkeypatch.setattr(mlanomaly, "_HAS_NUMPY", False)
    assert mlanomaly.multivariate_anomalies(_frame({"cpu": [1.0, 2.0]})) == []  # <3 buckets
    assert mlanomaly.multivariate_anomalies(_frame({"cpu": [1.0] * 5})) == []   # <co_min signals


def test_numpy_path_flags_co_deviation():
    pytest.importorskip("numpy")
    assert mlanomaly._HAS_NUMPY, "numpy importable -> module should use the mahalanobis path"
    res = mlanomaly.multivariate_anomalies(_co_deviation_frame(), z_floor=2.0, co_min=2,
                                           score_thresh=2.0)
    assert res
    assert res[0]["ts"] == 11 * 60.0
    assert {name for name, _z in res[0]["signals"]} >= {"cpu", "temp", "rtt"}
