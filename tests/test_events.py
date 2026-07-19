"""Edge/delta ext_events emitters: fire once on transition, quiet recovery, monotonic deltas,
and the collector probe-crash hook."""

import sqlite3

import pytest

from smokemon import collect, events


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(events, "_active", set())
    monkeypatch.setattr(events, "_counters", {})


def _evs(conn):
    return conn.execute("SELECT source,severity,event,detail FROM ext_events ORDER BY id").fetchall()


def test_trip_fires_once(node_db):
    for _ in range(3):
        events.trip(node_db, "k", source="host", severity="crit", event="overtemp", detail="hot")
    rows = _evs(node_db)
    assert len(rows) == 1 and rows[0][1] == "crit" and rows[0][2] == "overtemp"


def test_clear_only_after_trip(node_db):
    events.clear(node_db, "k", source="host")  # not active -> no-op
    assert _evs(node_db) == []
    events.trip(node_db, "k", source="host", severity="warn", event="swap-high", detail="x")
    events.clear(node_db, "k", source="host", event="swap-recovered", detail="ok")
    rows = _evs(node_db)
    assert [r[2] for r in rows] == ["swap-high", "swap-recovered"]
    assert rows[1][1] == "info"  # recovery is quiet -> not expedited, only in the 'all' filter


def test_edge_trip_then_clear(node_db):
    events.edge(node_db, True, "k", source="http", severity="warn", event="http-error", detail="500")
    events.edge(node_db, True, "k", source="http", severity="warn", event="http-error", detail="500")
    events.edge(node_db, False, "k", source="http", severity="warn", event="http-error",
                detail="x", clear_detail="ok")
    assert [r[2] for r in _evs(node_db)] == ["http-error", "http-error-recovered"]


def test_counter_delta_seeds_then_fires(node_db):
    kw = dict(source="host", severity="crit", event="oom-kill", detail_fn=lambda d: f"{d} new")
    events.counter(node_db, "oom", 5, **kw)
    assert _evs(node_db) == []          # first sight seeds silently
    events.counter(node_db, "oom", 7, **kw)
    rows = _evs(node_db)
    assert len(rows) == 1 and rows[0][3] == "2 new"
    events.counter(node_db, "oom", 7, **kw)
    assert len(_evs(node_db)) == 1      # unchanged -> nothing
    events.counter(node_db, "oom", 1, **kw)
    assert len(_evs(node_db)) == 1      # counter reset (reboot) re-seeds, no event
    events.counter(node_db, "oom", None, **kw)
    assert len(_evs(node_db)) == 1      # None (metric unavailable) -> ignored


def test_probe_crash_recorded_then_recovers(node_db, monkeypatch):
    monkeypatch.setattr(collect.governor, "should_shed", lambda name: (False, ""))

    def boom(conn):
        raise ValueError("nope")

    collect._guarded("ping", boom, node_db)()
    rows = _evs(node_db)
    assert len(rows) == 1 and rows[0][:3] == ("collector", "error", "probe-crash")
    assert "ValueError" in rows[0][3] and "ping" in rows[0][3]
    collect._guarded("ping", lambda c: None, node_db)()  # next success clears it (quiet)
    assert [r[2] for r in _evs(node_db)] == ["probe-crash", "probe-recovered"]


def test_uid_passes_through_trip_edge_and_counter(node_db):
    """uid links an event to whatever incident was open when it fired. None ships as NULL
    (unlinked evidence) rather than a guessed value."""
    events.trip(node_db, "k1", source="host", severity="crit", event="overtemp", detail="hot",
                uid="uid-1")
    events.edge(node_db, True, "k2", source="http", severity="warn", event="http-error",
                detail="500", uid="uid-2")
    events.counter(node_db, "oom", 1, source="host", severity="crit", event="oom-kill",
                   detail_fn=lambda d: f"{d} new")           # first sight: seeds silently
    events.counter(node_db, "oom", 2, source="host", severity="crit", event="oom-kill",
                   detail_fn=lambda d: f"{d} new", uid="uid-3")
    uids = dict(node_db.execute("SELECT event,uid FROM ext_events ORDER BY id").fetchall())
    assert uids == {"overtemp": "uid-1", "http-error": "uid-2", "oom-kill": "uid-3"}


def test_no_uid_is_unlinked_not_guessed(node_db):
    events.trip(node_db, "k", source="host", severity="warn", event="swap-high", detail="x")
    assert node_db.execute("SELECT uid FROM ext_events").fetchone() == (None,)


def test_db_contention_is_warn_not_crash(node_db, monkeypatch):
    """A transient 'database is locked' is contention, not a probe bug: one warn (edge), never a
    per-cycle error - so it can't spam or (with expedite ignoring collector events) cascade."""
    monkeypatch.setattr(collect.governor, "should_shed", lambda name: (False, ""))

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")

    collect._guarded("ping", locked, node_db)()
    collect._guarded("ping", locked, node_db)()  # still locked -> no second event (edge)
    rows = _evs(node_db)
    assert len(rows) == 1 and rows[0][:3] == ("collector", "warn", "db-contention")
    collect._guarded("ping", lambda c: None, node_db)()  # recovers quietly
    assert [r[2] for r in _evs(node_db)] == ["db-contention", "db-contention-recovered"]
