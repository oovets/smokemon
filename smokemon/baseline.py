"""Per-node learned baseline: an online estimate of what normal looks like on THIS box.

A fleet-wide threshold cannot know that one node sits behind a 4G modem where 90 ms is fine
and another is on fibre where 90 ms is a fault. The baseline supplies that per-node context so
a rule can say "far from normal for you" instead of only "past a fixed number".

Estimator: EWMA of the centre, plus EWMA of the absolute deviation as an online MAD surrogate.
Not a decaying-window median -- that requires keeping the window, which is exactly the memory
and disk cost this pivot exists to remove.

The decay constant is derived from the ACTUAL dt between samples:

    a = 1 - exp(-dt / tau)

so a signal fed every 10 s and one fed every 300 s decay at the same wall-clock rate. Deriving
`a` from a sample count instead would make fast signals learn 30x faster than slow ones, and
the central claim of the pivot -- that sampling rate stops being the important thing -- would
quietly stop holding right here.

This module does not know about incidents. `detect` decides whether an update is allowed;
`update()` simply refuses nothing. That boundary is what keeps the freeze policy in one place.
"""

from __future__ import annotations

import math
import sqlite3
import time

from . import config, core

# E|x - mu| -> MAD conversion for a normal distribution: MAD ~= 0.7979 * E|x-mu|.
_MEAN_ABS_TO_MAD = 0.7979
# robust_z scales MAD to a standard-deviation equivalent (the usual 1/0.6745).
_MAD_TO_SIGMA = 1.4826
# Below this the spread is unmeasurable rather than small; matches analyze.robust_z.
_DEGENERATE = 1e-9
_SATURATED = 50.0

_DDL = """
CREATE TABLE IF NOT EXISTS signal_baseline (
  signal  TEXT NOT NULL,
  entity  TEXT NOT NULL DEFAULT '',
  center  REAL NOT NULL,
  dev     REAL NOT NULL,
  n       INTEGER NOT NULL DEFAULT 0,
  updated REAL NOT NULL,
  PRIMARY KEY (signal, entity));
"""


class Baseline:
    """Learned centre and spread for one (signal, entity)."""

    __slots__ = ("center", "dev", "n", "updated", "dirty")

    def __init__(self, center: float = 0.0, dev: float = 0.0,
                 n: int = 0, updated: float = 0.0) -> None:
        self.center = center
        self.dev = dev
        self.n = n
        self.updated = updated
        self.dirty = False

    def ready(self, min_n: int) -> bool:
        return self.n >= min_n

    def scale(self, abs_floor: float, rel_floor: float) -> float:
        """The denominator for z, floored so a near-constant signal cannot manufacture huge
        z-scores from trivial wobble. Without the floor `dev` tends to 0 on a stable signal
        and a 0.1 ms jitter reads as z=200."""
        mad = self.dev * _MEAN_ABS_TO_MAD
        return max(mad * _MAD_TO_SIGMA, abs_floor, rel_floor * abs(self.center))

    def z(self, value: float, abs_floor: float, rel_floor: float) -> float:
        """Robust z, with the same degenerate handling as analyze.robust_z.

        A signal that is constant AND has no configured floor (a ratio pinned at 0.0 is the
        common case) gives scale == 0. Dividing there would raise, so the spread is treated as
        unmeasurable: identical to the centre reads as 0, anything else saturates. Saturating
        rather than returning inf keeps the value storable and comparable."""
        scale = self.scale(abs_floor, rel_floor)
        delta = value - self.center
        if scale < _DEGENERATE:
            if abs(delta) < _DEGENERATE:
                return 0.0
            return _SATURATED if delta > 0 else -_SATURATED
        return delta / scale


_cache: dict[tuple[str, str], Baseline] = {}
_loaded = False
_last_flush = 0.0


def _alpha(dt: float, tau: float) -> float:
    """Wall-clock-proportional EWMA weight. Clamped: a dt of hours (node asleep, clock step)
    would otherwise give a=1.0 and throw away everything learned so far in a single sample."""
    if dt <= 0.0 or tau <= 0.0:
        return 1.0
    return min(1.0, 1.0 - math.exp(-dt / tau))


def ensure_table(conn) -> None:
    """Node-local, deliberately NOT in schema._BODY: membership there is the ship switch, and
    a learned baseline is local working state the hub has no use for. Same reasoning as
    logexcerpt's log_cursors."""
    conn.executescript(_DDL)


def load(conn) -> None:
    """Seed the cache from disk once per process."""
    global _loaded, _last_flush
    if _loaded:
        return
    ensure_table(conn)
    try:
        rows = conn.execute(
            "SELECT signal, entity, center, dev, n, updated FROM signal_baseline").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for sig, ent, center, dev, n, updated in rows:
        _cache[(sig, ent or "")] = Baseline(center, dev, int(n), updated)
    _loaded = True
    _last_flush = time.time()


def get(signal: str, entity: str = "") -> Baseline:
    b = _cache.get((signal, entity))
    if b is None:
        b = _cache[(signal, entity)] = Baseline()
    return b


def update(signal: str, entity: str, value: float, wall: float,
           tau: float | None = None, gate_z: float | None = None,
           abs_floor: float = 0.0, rel_floor: float = 0.0) -> Baseline:
    """Fold one sample into the baseline. Callers must only call this while the signal's
    detector state is OK -- see detect.py. Returns the updated baseline.

    Outlier gating: a sample beyond `gate_z` still updates, but with its weight divided by
    |z|. A single spike that never persists long enough to arm an incident should not drag
    the centre with it, while a genuine sustained shift still gets there (each of its samples
    is gated less as the centre moves toward it)."""
    tau = config.BASELINE_TAU_S if tau is None else tau
    gate_z = config.BASELINE_GATE_Z if gate_z is None else gate_z
    b = get(signal, entity)

    if b.n == 0:
        b.center, b.dev, b.n, b.updated, b.dirty = value, 0.0, 1, wall, True
        return b

    a = _alpha(wall - b.updated, tau)
    if b.n > 1 and gate_z > 0.0:
        z = abs(b.z(value, abs_floor, rel_floor))
        if z > gate_z:
            a /= z  # winsorise: the further out, the less it moves us

    dist = abs(value - b.center)
    b.center += a * (value - b.center)
    b.dev += a * (dist - b.dev)
    b.n = min(b.n + 1, config.BASELINE_MAX_N)
    b.updated = wall
    b.dirty = True
    return b


def maybe_flush(conn, now: float, interval: float | None = None, force: bool = False) -> int:
    """Persist dirty baselines, at most every BASELINE_FLUSH_S. Returns rows written.

    Deliberately not per sample: that would be ~8600 extra commits a day on a node whose whole
    point is to stop writing constantly, and would spend the entire SD-write budget on
    bookkeeping. The accepted cost is that a crash loses up to one flush interval of learning
    -- `n` can go backwards and warmup can lengthen after a restart. An EWMA with a one-day
    tau does not notice 15 minutes; the warmup counter is the part that actually regresses."""
    global _last_flush
    interval = config.BASELINE_FLUSH_S if interval is None else interval
    if not force and now - _last_flush < interval:
        return 0
    _last_flush = now
    dirty = [(s, e, b) for (s, e), b in _cache.items() if b.dirty]
    if not dirty:
        return 0
    try:
        conn.executemany(
            "INSERT INTO signal_baseline (signal, entity, center, dev, n, updated) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(signal, entity) DO UPDATE SET "
            "center=excluded.center, dev=excluded.dev, n=excluded.n, updated=excluded.updated",
            [(s, e, b.center, b.dev, b.n, b.updated) for s, e, b in dirty])
        conn.commit()
    except sqlite3.OperationalError as exc:
        core.log(f"baseline: flush deferred: {exc}")
        return 0
    for _s, _e, b in dirty:
        b.dirty = False
    return len(dirty)


def thaw(signal: str, entity: str = "") -> None:
    """Forget what we learned for this signal so it can relearn from the current regime.

    Only ever called for RELATIVE rules whose incident expired (see detect: an absolute
    safety rule must never be trained away -- a disk at 96% is still bad after 24 hours, and
    thawing there would silently turn a permanent fault into the new normal)."""
    _cache.pop((signal, entity), None)


def reset() -> None:
    """Tests only."""
    global _loaded, _last_flush
    _cache.clear()
    _loaded = False
    _last_flush = 0.0
