"""Footprint governor (node-side, opt-in, stdlib).

The project's edge promise is "max detail until it costs too much, then back off". This module
is the back-off: when the collector process exceeds a configured RSS or DB-size budget, the
scheduler skips its least essential probes for that cycle and records a throttled governor
event, so detail degrades gracefully instead of the footprint overrunning target. Both budgets
default to 0 (disabled), so default behaviour is unchanged.

Under incident-centric storage this is no longer the main pressure valve -- a healthy node
barely writes, and the hard caps that matter most are INCIDENT_DURING_MAX, INCIDENT_MAX_OPEN
and the signal registry ceiling, all of which bound cost by construction rather than by
reacting to it. What is left for the governor is the two probes that still cost real bytes."""

import os
import resource
import time

from . import config, schema

_PAGE = resource.getpagesize()

# Probes eligible to be shed, costliest-first. Everything else always runs.
#
# logexcerpt writes by far the largest rows on the node (up to LOGEXCERPT_MAX_BYTES of text per
# capture), and shedding it costs detail on an incident rather than the incident itself -- the
# transition is still recorded, just without the log tail. inventory is cheap but entirely
# deferrable: it is delta-coded device facts, and a fact that changed while we were over budget
# is still there to be noticed on the next cycle.
#
# ping, net, host, heartbeat and the detector sweep are never shed. Shedding detection to save
# footprint would mean a node under memory pressure -- exactly when something is wrong -- going
# quiet about it.
EXPENSIVE = ("logexcerpt", "inventory")

_NOTE_INTERVAL = 60.0   # throttle governor event rows to at most one per minute
_last_note = 0.0


def rss_mb() -> float | None:
    """Resident set of this process in MB, live from /proc/self/statm. Falls back to peak
    ru_maxrss, which on Linux is KiB (it is bytes on BSD -- the old cross-platform version
    divided by 1e6 unconditionally and so under-reported the fallback by ~1000x here)."""
    try:
        with open("/proc/self/statm") as f:
            return round(int(f.read().split()[1]) * _PAGE / 1e6, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (OSError, ValueError):
        return None


def db_mb(path: str | None = None) -> float | None:
    """Node DB size including WAL/SHM sidecars, in MB."""
    path = config.DB_PATH if path is None else path
    total = 0
    found = False
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
            found = True
        except OSError:
            pass
    return round(total / 1e6, 1) if found else None


def over_budget() -> tuple[bool, str]:
    """(over, reason). True when any enabled budget is exceeded."""
    reasons = []
    if config.MAX_RSS_MB > 0:
        r = rss_mb()
        if r is not None and r > config.MAX_RSS_MB:
            reasons.append(f"rss {r}>{config.MAX_RSS_MB:.0f}MB")
    if config.MAX_DB_MB > 0:
        d = db_mb()
        if d is not None and d > config.MAX_DB_MB:
            reasons.append(f"db {d}>{config.MAX_DB_MB:.0f}MB")
    return (bool(reasons), "; ".join(reasons))


def should_shed(name: str) -> tuple[bool, str]:
    """Whether probe `name` should be skipped this cycle. Only expensive probes are shed, and
    only when a budget is breached."""
    if name not in EXPENSIVE:
        return False, ""
    return over_budget()


def note(conn, name: str, reason: str) -> None:
    """Record a shed as an ext_events row (ships to the hub), throttled so a sustained breach
    doesn't spam the table."""
    global _last_note
    now = time.time()
    if now - _last_note < _NOTE_INTERVAL:
        return
    _last_note = now
    schema.insert(conn, "ext_events", [{"ts": now, "source": "governor", "severity": "warn",
                                        "event": "shed", "detail": f"{name}: {reason}"}])
    conn.commit()
