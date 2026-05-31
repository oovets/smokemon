"""Hub response cache: expensive aggregates are memoized for HUB_CACHE_TTL_S so the dashboard's
repeated/concurrent/multi-user polls don't each recompute from scratch."""

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


def test_memoizes_within_ttl(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 60)
    p = _Counter()
    assert hub._cached("k", p) == {"v": 1}
    assert hub._cached("k", p) == {"v": 1}  # served from cache
    assert p.n == 1                          # producer ran once


def test_recomputes_after_ttl(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(hub, "time", SimpleNamespace(time=lambda: clock["t"]))
    p = _Counter()
    hub._cached("k", p)
    clock["t"] += 61                         # past the TTL
    hub._cached("k", p)
    assert p.n == 2


def test_distinct_keys_dont_share(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 60)
    p = _Counter()
    assert hub._cached("risks:24", p) == {"v": 1}
    assert hub._cached("services", p) == {"v": 2}


def test_ttl_zero_disables_cache(monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 0)
    p = _Counter()
    hub._cached("k", p)
    hub._cached("k", p)
    assert p.n == 2                          # no memoization when disabled
