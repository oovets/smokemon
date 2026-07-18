"""Self-instrumentation: summed-pid RSS and the SD-write-rate metric (the wear regression guard).

rss is summed over all smokemon pids and write_mb_day is the projected SD-write load, so card
wear is as visible (and as testable) as RSS. These tests drive the rate math directly,
independent of /proc availability."""

from smokemon.probes import host


def _reset():
    host._prev_self_cpu = None
    host._prev_self_io = None


def test_write_rate_seeds_then_projects(monkeypatch):
    _reset()
    state = {"bytes": 1_000_000, "now": 1000.0}
    monkeypatch.setattr(host, "_fleet_footprint_linux", lambda: (42.0, state["bytes"]))
    monkeypatch.setattr(host.time, "time", lambda: state["now"])

    first = host._self_proc(dt=10.0)
    assert first["rss_mb"] == 42.0           # summed-pid RSS, not this process's ru_maxrss
    assert first["write_mb_day"] is None     # first sample only seeds the cursor

    # one hour later, +3.6 MB written -> 3.6 MB/h -> ~86.4 MB/day
    state["bytes"] += 3_600_000
    state["now"] += 3600.0
    second = host._self_proc(dt=10.0)
    assert second["write_mb_day"] == 86.4


def test_write_rate_ignores_counter_reset(monkeypatch):
    """A restarted pid resets write_bytes; the summed counter can drop. That must read as
    'unknown' (None), never a bogus negative rate - the regression guard against wear noise."""
    _reset()
    state = {"bytes": 5_000_000, "now": 2000.0}
    monkeypatch.setattr(host, "_fleet_footprint_linux", lambda: (30.0, state["bytes"]))
    monkeypatch.setattr(host.time, "time", lambda: state["now"])
    host._self_proc(dt=10.0)                 # seed
    state["bytes"] = 1_000_000               # pid churn -> counter went backwards
    state["now"] += 60.0
    out = host._self_proc(dt=10.0)
    assert out["write_mb_day"] is None
