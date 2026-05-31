"""Cadence jitter: stable per-node offset in [0, interval/4) so the fleet doesn't fire in lockstep."""

from smokemon import core


def test_offset_within_quarter_interval():
    for node in ("a", "pi-zero-01", "jetson", "app01"):
        off = core._jitter(60.0, node)
        assert 0.0 <= off < 15.0  # [0, interval/4)


def test_stable_across_calls():
    assert core._jitter(60.0, "pi-01") == core._jitter(60.0, "pi-01")


def test_differs_between_nodes():
    offs = {core._jitter(60.0, n) for n in ("n1", "n2", "n3", "n4", "n5")}
    assert len(offs) > 1  # not all identical -> the herd actually spreads


def test_no_offset_when_disabled():
    assert core._jitter(0.0, "pi-01") == 0.0    # interval<=0
    assert core._jitter(60.0, "") == 0.0        # no node identity


def test_next_due_is_interval_aligned_plus_offset():
    """The scheduler computes due = (int((now-off)//interval)+1)*interval + off. Verify that
    boundary is periodic with the node's offset, so jitter shifts the phase without drifting."""
    interval, now = 60.0, 1000.0
    off = core._jitter(interval, "pi-01")
    due = (int((now - off) // interval) + 1) * interval + off
    assert due > now
    assert abs((due - off) % interval) < 1e-6  # lands exactly on k*interval + offset
