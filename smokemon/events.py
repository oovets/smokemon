"""Edge-triggered ext_events emitters for the collectors.

A condition fires ONE event when it goes bad (and a quiet 'recovered' when it clears), so a
persistent problem never re-floods the events table or the ship wire every cycle. All inputs are
values the probes already computed - no new probing, sockets, or files, so this adds no footprint.
State is in-memory per collector process (resets on restart, which is fine: a still-bad condition
re-trips on its next observation).

severity convention: bad states use warn/error/crit (elevated -> shown in the logs tab + expedited
to the hub); the paired recovery uses info (quiet -> visible only in the 'all' filter)."""

import time

from . import schema

_active: set[str] = set()        # keys currently in their bad state (so trip fires once)
_counters: dict[str, int] = {}   # last seen value of a monotonic counter, per key


def _emit(conn, source: str, severity: str, event: str, detail: str) -> None:
    schema.insert(conn, "ext_events",
                  [{"ts": time.time(), "source": source, "severity": severity,
                    "event": event, "detail": detail}])
    conn.commit()


def trip(conn, key: str, *, source: str, severity: str, event: str, detail: str) -> None:
    """Fire `event` once when `key` enters its bad state; a no-op while it stays bad."""
    if key in _active:
        return
    _active.add(key)
    _emit(conn, source, severity, event, detail)


def clear(conn, key: str, *, source: str, event: str = "recovered", detail: str = "") -> None:
    """Fire a quiet (info) recovery once when `key` leaves its bad state; a no-op otherwise."""
    if key not in _active:
        return
    _active.discard(key)
    _emit(conn, source, "info", event, detail)


def edge(conn, bad: bool, key: str, *, source: str, severity: str, event: str,
         detail: str, clear_detail: str = "") -> None:
    """Trip when `bad`, clear when not - the common 'condition currently true?' case."""
    if bad:
        trip(conn, key, source=source, severity=severity, event=event, detail=detail)
    else:
        clear(conn, key, source=source, event=event + "-recovered", detail=clear_detail)


def counter(conn, key: str, value, *, source: str, severity: str, event: str, detail_fn) -> None:
    """For monotonic counters (OOM kills, throttle counts): fire when `value` rises above the last
    seen, reporting the delta via detail_fn(delta). First sight seeds silently (no event for a
    pre-existing count). A drop (counter reset, e.g. reboot) re-seeds without firing."""
    if value is None:
        return
    prev = _counters.get(key)
    _counters[key] = value
    if prev is not None and value > prev:
        _emit(conn, source, severity, event, detail_fn(value - prev))
