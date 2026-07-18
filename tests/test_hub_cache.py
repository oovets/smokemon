"""Hub response cache: expensive aggregates are memoized for HUB_CACHE_TTL_S so the dashboard's
repeated/concurrent/multi-user polls don't each recompute from scratch.

Part of every cache key comes from the query string, so the bound on the cache is a
availability property, not a tidiness one: without it, anyone who can reach a GET endpoint can
grow the hub's memory until it is OOM-killed.
"""

from types import SimpleNamespace

from smokemon import config, hub


def _reset():
    hub._resp_cache.clear()
    hub._resp_cache_locks.clear()


class _Counter:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return {"v": self.n}


# ---------- memoization ----------

def test_memoizes_within_ttl(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 60)
    p = _Counter()
    assert hub._cached("k", p) == {"v": 1}
    assert hub._cached("k", p) == {"v": 1}   # served from cache
    assert p.n == 1


def test_recomputes_after_ttl(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(hub, "time", SimpleNamespace(time=lambda: clock["t"]))
    p = _Counter()
    hub._cached("k", p)
    clock["t"] += 61
    hub._cached("k", p)
    assert p.n == 2


def test_distinct_keys_dont_share(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 60)
    p = _Counter()
    assert hub._cached("density:24", p) == {"v": 1}
    assert hub._cached("cost:24", p) == {"v": 2}


def test_ttl_zero_disables_cache(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 0)
    p = _Counter()
    hub._cached("k", p)
    hub._cached("k", p)
    assert p.n == 2                          # the global kill-switch


# ---------- the bound ----------

def test_cache_does_not_grow_without_bound(monkeypatch):
    """`node` reaches the key straight from the query string. A dict keyed on caller-supplied
    text is an unbounded allocation an anonymous GET can drive, so the LRU has a hard cap."""
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 600)
    p = _Counter()
    for i in range(hub._RESP_CACHE_MAX * 4):
        hub._cached(f"logs:attacker-{i}:elevated:24", p)
    assert len(hub._resp_cache) <= hub._RESP_CACHE_MAX
    # the lock dict is evicted alongside the value, or it becomes the leak the cap prevents
    assert len(hub._resp_cache_locks) <= hub._RESP_CACHE_MAX


def test_eviction_is_least_recently_used(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 600)
    p = _Counter()
    hub._cached("keeper", p)
    for i in range(hub._RESP_CACHE_MAX - 1):
        hub._cached(f"filler-{i}", p)
    hub._cached("keeper", p)                 # a hit refreshes its recency
    hub._cached("newcomer", p)               # forces one eviction
    assert "keeper" in hub._resp_cache
    assert "filler-0" not in hub._resp_cache  # the genuinely coldest key went


def test_hours_are_quantised_before_reaching_the_key():
    """A float straight off the query string is an unbounded set of keys (?hours=1.0000001 ad
    infinitum), so the window is snapped to a small ladder first."""
    seen = {hub._clamp_hours({"hours": [str(1 + i / 1000)]}) for i in range(500)}
    assert seen <= set(hub._HOUR_BUCKETS)
    assert len(seen) <= 2


def test_quantised_hours_never_narrow_the_window():
    """Snapping rounds UP: the caller may get more data than asked for, never less."""
    for raw in ("0.5", "2", "7", "25", "100", "500"):
        assert hub._clamp_hours({"hours": [raw]}) >= float(raw)


def test_clamp_hours_bounds_and_defaults():
    assert hub._clamp_hours({"hours": ["1e9"]}) == float(hub._MAX_HOURS)  # huge value clamped
    assert hub._clamp_hours({"hours": ["-5"]}) == 1.0                     # negative floored
    assert hub._clamp_hours({"hours": ["abc"]}) == 24.0                   # non-numeric -> default
    assert hub._clamp_hours({}) == 24.0
    assert hub._clamp_hours({"hours": ["6"]}) == 6.0


def test_clamp_int_bounds_and_defaults():
    assert hub._clamp_int({"limit": ["50"]}, "limit", 200, 1, 1000) == 50
    assert hub._clamp_int({"limit": ["99999"]}, "limit", 200, 1, 1000) == 1000
    assert hub._clamp_int({"limit": ["0"]}, "limit", 200, 1, 1000) == 1
    assert hub._clamp_int({"limit": ["abc"]}, "limit", 200, 1, 1000) == 200
    assert hub._clamp_int({}, "limit", 200, 1, 1000) == 200
