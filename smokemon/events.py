"""Edge-triggered ext_events emitters for the collectors.

A condition fires ONE event when it goes bad (and a quiet 'recovered' when it clears), so a
persistent problem never re-floods the events table or the ship wire every cycle. All inputs are
values the probes already computed - no new probing, sockets, or files, so this adds no footprint.
State is in-memory per collector process (resets on restart, which is fine: a still-bad condition
re-trips on its next observation).

severity convention: bad states use warn/error/crit (elevated -> shown in the logs tab + expedited
to the hub); the paired recovery uses info (quiet -> visible only in the 'all' filter)."""

import sqlite3
import time

from . import core, schema

_active: set[str] = set()        # keys currently in their bad state (so trip fires once)
_counters: dict[str, int] = {}   # last seen value of a monotonic counter, per key


def _emit(conn, source: str, severity: str, event: str, detail: str, uid: str | None = None) -> bool:
    """Record one ext_events row. Best-effort: if the DB is contended (the very thing some of
    these events report), swallow the lock error rather than cascade - the condition re-trips on
    its next observation. Returns True iff the row was written (so callers only mark state then).

    uid links this event to whatever incident the caller says was relevant (exact for
    incidents.py's own open/close, a best-effort incidents.active_uid() elsewhere). Left None,
    it ships as unlinked evidence rather than guessed."""
    try:
        schema.insert(conn, "ext_events",
                      [{"ts": time.time(), "source": source, "severity": severity,
                        "event": event, "detail": detail, "uid": uid}])
        conn.commit()
        return True
    except sqlite3.OperationalError as e:
        core.log(f"events: could not record {event}: {e}")
        return False


def trip(conn, key: str, *, source: str, severity: str, event: str, detail: str,
         uid: str | None = None) -> None:
    """Fire `event` once when `key` enters its bad state; a no-op while it stays bad. State is
    marked only on a successful write, so a write that loses to DB contention retries next cycle."""
    if key in _active:
        return
    if _emit(conn, source, severity, event, detail, uid=uid):
        _active.add(key)


def clear(conn, key: str, *, source: str, event: str = "recovered", detail: str = "",
          uid: str | None = None) -> None:
    """Fire a quiet (info) recovery once when `key` leaves its bad state; a no-op otherwise."""
    if key not in _active:
        return
    if _emit(conn, source, "info", event, detail, uid=uid):
        _active.discard(key)


def edge(conn, bad: bool, key: str, *, source: str, severity: str, event: str,
         detail: str, clear_detail: str = "", uid: str | None = None) -> None:
    """Trip when `bad`, clear when not - the common 'condition currently true?' case."""
    if bad:
        trip(conn, key, source=source, severity=severity, event=event, detail=detail, uid=uid)
    else:
        clear(conn, key, source=source, event=event + "-recovered", detail=clear_detail, uid=uid)


def counter(conn, key: str, value, *, source: str, severity: str, event: str, detail_fn,
            uid: str | None = None) -> None:
    """For monotonic counters (OOM kills, throttle counts): fire when `value` rises above the last
    seen, reporting the delta via detail_fn(delta). First sight seeds silently (no event for a
    pre-existing count). A drop (counter reset, e.g. reboot) re-seeds without firing."""
    if value is None:
        return
    prev = _counters.get(key)
    _counters[key] = value
    if prev is not None and value > prev:
        _emit(conn, source, severity, event, detail_fn(value - prev), uid=uid)
