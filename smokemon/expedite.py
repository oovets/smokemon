"""Out-of-band ship trigger: when a new elevated ext_events row lands, ship immediately so errors
reach the hub in seconds instead of on the next bulk ship tick - decoupling error-delivery latency
from the (possibly long) metric ship cadence.

Event-driven and cheap: each check is one indexed MAX(id) read; a real ship runs only when an
elevated row actually appeared, at most one in flight at a time, no faster than the check interval.
A no-op when no hubs are configured or SMOKEMON_SHIP_EXPEDITE=0. The ship runs on a short-lived
daemon thread so a slow/hung POST never stalls the collector loop it is hooked into.
"""

from __future__ import annotations

import threading

from . import config, core, ship
from .probes.logexcerpt import is_elevated

_seen_id: int | None = None        # high-water mark of ext_events already examined
_pending = False                   # an elevated event is waiting for a ship (level, not edge)
_inflight = threading.Lock()       # coalesce: at most one expedite ship at a time


def should_ship(conn) -> bool:
    """True while an elevated ext_events row is waiting to be shipped.

    This is a LEVEL, not an edge. A correlated storm -- a thermal throttle tripping temperature,
    loss and latency at once -- raises the level once and costs one ship, not one per incident.
    The flag is only cleared when a ship actually starts, so a detection that arrives while an
    expedite is already in flight is not lost: the previous ship's gather() may already have run,
    and clearing on detection would leave those rows to wait for the bulk tick.

    The first call only seeds the high-water mark, so a pre-existing backlog is never expedited
    on startup - the bulk shipper carries that."""
    global _seen_id, _pending
    row = conn.execute("SELECT COALESCE(MAX(id),0) FROM ext_events").fetchone()
    cur_max = int(row[0]) if row else 0
    first = _seen_id is None
    prev = _seen_id or 0
    _seen_id = cur_max
    if first:
        return False
    if cur_max > prev:
        for source, sev in conn.execute(
                "SELECT source, severity FROM ext_events WHERE id>? ORDER BY id", (prev,)):
            # The collector's own events (probe-crash / db-contention) must NOT trigger expedite:
            # an expedited ship is another local writer, so reacting to a local DB-contention
            # event would add write pressure and feed a crash->ship->crash loop. Those ride the
            # normal bulk tick.
            if source == "collector":
                continue
            if is_elevated(sev):
                _pending = True
                break
    return _pending


def _run() -> None:
    try:
        n = ship.expedite()
        if n:
            core.log(f"expedite: shipped {n} rows")
    finally:
        try:
            _inflight.release()
        except RuntimeError:
            pass


def check(conn) -> None:
    """Collector hook (registered on the fast loop). Cheap no-op unless an elevated event landed
    and no expedite is already running; then fire a one-shot ship on a daemon thread."""
    global _pending
    if not config.SHIP_EXPEDITE or not config.HUBS:
        return
    if not should_ship(conn):
        return
    if not _inflight.acquire(blocking=False):
        return  # already shipping; _pending stays raised so the next check re-arms if needed
    _pending = False   # cleared only now, once a ship is actually starting
    core.log("expedite: elevated event pending -> shipping now")
    threading.Thread(target=_run, name="smokemon-expedite", daemon=True).start()
