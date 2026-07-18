"""Bounded in-memory signal registry. The node's working memory, and the only place a
sample lives while things are normal.

A *signal* is a named scalar series a probe produces as a side effect of what it already
computes: (name, entity, value). `name` is the rule namespace ("ping.loss", "host.temp");
`entity` is the per-instance discriminator (target, mount, interface), canonicalised by the
probe so an interface alias or a mount-path variant does not silently become a second signal
with its own cold baseline.

Nothing here touches SQLite, shipping, incident policy or rules. It holds the pre-incident
window so that a detector, at the moment it confirms an anomaly, can reach backwards for the
baseline it needs -- without any of those samples ever having been written to disk.

Memory is bounded by construction, not by convention:

    SIGNAL_MAX * SIGNAL_RING * 3 * 8 bytes  =  48 * 64 * 24  =  73.7 KB

Three parallel array('d') per signal rather than a deque of tuples: 8 bytes per element with
no per-element Python object, against ~120 bytes for a tuple of two floats. That is the
difference between ~74 KB and ~250 KB of steady-state RSS, and RSS is a number smokemon
publishes about itself -- the observer must not move the thing it measures.
"""

from __future__ import annotations

import time
from array import array

from . import config

# Wall-clock and monotonic timestamps are kept side by side for every sample. Storage and
# display want wall clock; every duration the detector reasons about (debounce, hysteresis
# hold, cooldown) must be monotonic, because Pi and Jetson nodes NTP-step at boot, often by
# hours. A debounce measured on wall clock would either fire instantly or never.
_SLOTS = 3


class Ring:
    """Fixed-capacity circular buffer of (wall, mono, value) triples."""

    __slots__ = ("_wall", "_mono", "_val", "_i", "_n", "_cap")

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._wall = array("d", bytes(8 * cap))
        self._mono = array("d", bytes(8 * cap))
        self._val = array("d", bytes(8 * cap))
        self._i = 0   # next write position
        self._n = 0   # samples held, saturating at cap

    def push(self, wall: float, mono: float, value: float) -> None:
        i = self._i
        self._wall[i] = wall
        self._mono[i] = mono
        self._val[i] = value
        self._i = (i + 1) % self._cap
        if self._n < self._cap:
            self._n += 1

    def __len__(self) -> int:
        return self._n

    def last(self) -> tuple[float, float, float] | None:
        """Most recent (wall, mono, value), or None when empty."""
        if not self._n:
            return None
        i = (self._i - 1) % self._cap
        return (self._wall[i], self._mono[i], self._val[i])

    def tail(self, k: int) -> list[tuple[float, float]]:
        """Up to the last `k` samples as [(wall_ts, value)], oldest first.

        Copies out of the ring so the caller holds no reference into a buffer that keeps being
        overwritten, and so the copy cannot stall the next feed() for longer than the copy."""
        k = min(k, self._n)
        if k <= 0:
            return []
        start = (self._i - k) % self._cap
        out = []
        for j in range(k):
            i = (start + j) % self._cap
            out.append((self._wall[i], self._val[i]))
        return out

    def _ordered(self) -> list[int]:
        """Indices oldest-first."""
        start = (self._i - self._n) % self._cap
        return [(start + j) % self._cap for j in range(self._n)]

    def before(self, mono_cutoff: float, k: int) -> list[tuple[float, float]]:
        """Up to the last `k` samples strictly older than `mono_cutoff`, oldest first.

        This is the pre-incident baseline. The cutoff is the moment the breach STARTED, not
        the moment it was confirmed: the samples in between are already anomalous, and calling
        them baseline would put the anomaly inside its own reference window."""
        out = [(self._wall[i], self._val[i]) for i in self._ordered()
               if self._mono[i] < mono_cutoff]
        return out[-k:] if k > 0 else []

    def since(self, mono_cutoff: float, k: int) -> list[tuple[float, float]]:
        """Up to the first `k` samples at or after `mono_cutoff`, oldest first. The onset of
        the anomaly -- how it came on is often the most diagnostic part of an incident."""
        out = [(self._wall[i], self._val[i]) for i in self._ordered()
               if self._mono[i] >= mono_cutoff]
        return out[:k] if k > 0 else []


_rings: dict[tuple[str, str], Ring] = {}
_drops = 0            # feeds rejected because the registry is full
_last_drop_warn: float | None = None   # None = never warned, so the first drop always speaks


def key(name: str, entity: str = "") -> tuple[str, str]:
    return (name, entity or "")


def feed(name: str, entity: str = "", value: float | None = None,
         wall: float | None = None, mono: float | None = None) -> tuple[float, float] | None:
    """Record one sample. Returns (wall, mono) for the sample, or None if it was dropped.

    A None value is dropped silently: probes routinely have a metric they could not read this
    cycle, and a gap is not an anomaly. Feeding an unknown signal once the registry is full is
    also a drop -- a node churning container names or interface aliases must not be able to
    grow this dict without bound, so the cap is enforced here rather than trusted upstream."""
    global _drops
    if value is None:
        return None
    k = key(name, entity)
    ring = _rings.get(k)
    if ring is None:
        if len(_rings) >= config.SIGNAL_MAX:
            _drops += 1
            return None
        ring = _rings[k] = Ring(config.SIGNAL_RING)
    w = time.time() if wall is None else wall
    m = time.monotonic() if mono is None else mono
    ring.push(w, m, float(value))
    return (w, m)


def ring(name: str, entity: str = "") -> Ring | None:
    return _rings.get(key(name, entity))


def keys() -> list[tuple[str, str]]:
    return list(_rings)


def drops() -> int:
    return _drops


def should_warn_drops(now: float, interval: float = 3600.0) -> bool:
    """True at most once per `interval` while feeds are being dropped. Dropping is a real
    loss of coverage, so it must be visible -- but a node in that state is dropping every
    cycle, and an unthrottled warning would itself become the flood."""
    global _last_drop_warn
    if not _drops:
        return False
    if _last_drop_warn is not None and now - _last_drop_warn < interval:
        return False
    _last_drop_warn = now
    return True


def stats() -> tuple[int, int]:
    """(signal_count, approximate_bytes). Reported by the heartbeat so the memory bound is
    observable in production rather than only asserted in a test."""
    n = len(_rings)
    payload = n * config.SIGNAL_RING * _SLOTS * 8
    overhead = n * 200  # dict entry + Ring object + three array headers, measured empirically
    return (n, payload + overhead)


def reset() -> None:
    """Drop all state. Tests only -- the registry is process-lifetime in production."""
    global _drops, _last_drop_warn
    _rings.clear()
    _drops = 0
    _last_drop_warn = None
