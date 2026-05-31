"""Hub log/event surface (hubapi.events_log), shipper events-first ordering, and the
expedite-on-error trigger. Pure/unit level - no real network (the HTTP route is covered in
test_hub.py with the in-process server)."""

import threading
import time

import pytest

from smokemon import config, core, expedite, hubapi, schema, ship
from smokemon.probes.logexcerpt import is_elevated


@pytest.fixture
def hub_conn(hub_db):
    conn = core.connect(str(hub_db))
    schema.init_hub(conn)
    yield conn
    conn.close()


def _seed(conn, now):
    def ev(node, sev, event, detail, dt):
        schema.insert(conn, "ext_events",
                      [{"ts": now - dt, "source": "governor", "severity": sev, "event": event,
                        "detail": detail}], node=node)
    ev("pi01", "info", "tick", "routine", 60)
    ev("pi01", "warn", "shed", "mtr: over budget", 50)
    ev("pi02", "error", "scrape-failed", "https://x timeout", 40)
    schema.insert(conn, "log_excerpts",
                  [{"ts": now - 30, "source": "syslog", "path": "/var/log/syslog",
                    "reason": "ext_event:ext/scrape-failed", "bytes": 9, "dropped": 128,
                    "excerpt": "x\n" * 3000}], node="pi02")
    conn.commit()


# ---- hubapi.events_log -------------------------------------------------------------------

def test_events_log_severity_filter(hub_conn):
    now = time.time()
    _seed(hub_conn, now)
    assert len(hubapi.events_log(hub_conn, None, "all", 24, now=now)["rows"]) == 4  # 3 events + 1 log
    elevated = hubapi.events_log(hub_conn, None, "elevated", 24, now=now)["rows"]
    assert len(elevated) == 3  # info event dropped; warn + error + log kept
    assert not any(r["kind"] == "event" and r["sev"] < 2 for r in elevated)
    error = hubapi.events_log(hub_conn, None, "error", 24, now=now)["rows"]
    assert len(error) == 1 and error[0]["kind"] == "event" and error[0]["severity"] == "error"


def test_events_log_node_filter_and_newest_first(hub_conn):
    now = time.time()
    _seed(hub_conn, now)
    rows = hubapi.events_log(hub_conn, "pi02", "all", 24, now=now)["rows"]
    assert {r["node"] for r in rows} == {"pi02"}
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)


def test_events_log_truncates_excerpt(hub_conn):
    now = time.time()
    _seed(hub_conn, now)
    log = next(r for r in hubapi.events_log(hub_conn, None, "elevated", 24, now=now)["rows"]
               if r["kind"] == "log")
    assert log["truncated"] is True and len(log["detail"]) == hubapi._LOG_PREVIEW
    assert log["dropped"] == 128


def test_events_log_limit(hub_conn):
    now = time.time()
    _seed(hub_conn, now)
    assert len(hubapi.events_log(hub_conn, None, "all", 24, limit=2, now=now)["rows"]) == 2


# ---- shipper events-first ordering -------------------------------------------------------

def test_ordered_tables_priority():
    ordered = ship._ordered_tables()
    assert ordered[0] == "ext_events" and ordered[1] == "log_excerpts"
    assert set(ordered) == set(schema.STD_TABLES)  # every table still present, no dupes/drops


# ---- expedite trigger --------------------------------------------------------------------

def test_is_elevated():
    assert is_elevated("error") and is_elevated("warn") and is_elevated("CRIT") and is_elevated("oops")
    assert not is_elevated("info") and not is_elevated("") and not is_elevated(None)


def test_should_ship_seeds_then_fires(node_db, monkeypatch):
    monkeypatch.setattr(expedite, "_seen_id", None)
    schema.insert(node_db, "ext_events", [{"ts": time.time(), "source": "x", "severity": "info",
                                           "event": "tick", "detail": ""}])
    node_db.commit()
    assert expedite.should_ship(node_db) is False  # first call only seeds the high-water mark
    schema.insert(node_db, "ext_events", [{"ts": time.time(), "source": "gov", "severity": "error",
                                           "event": "boom", "detail": "d"}])
    node_db.commit()
    assert expedite.should_ship(node_db) is True   # a new elevated row appeared
    assert expedite.should_ship(node_db) is False  # nothing new since


def test_should_ship_ignores_quiet(node_db, monkeypatch):
    monkeypatch.setattr(expedite, "_seen_id", None)
    expedite.should_ship(node_db)  # seed
    schema.insert(node_db, "ext_events", [{"ts": time.time(), "source": "x", "severity": "info",
                                           "event": "tick", "detail": ""}])
    node_db.commit()
    assert expedite.should_ship(node_db) is False  # only an info row -> no expedite


def test_check_noop_without_hubs(node_db, monkeypatch):
    fired = []
    monkeypatch.setattr(config, "HUBS", [])
    monkeypatch.setattr(config, "SHIP_EXPEDITE", True)
    monkeypatch.setattr(expedite, "should_ship", lambda c: True)
    monkeypatch.setattr(ship, "expedite", lambda: fired.append(1))
    expedite.check(node_db)
    assert fired == []  # no hubs -> never ships


def test_check_noop_when_disabled(node_db, monkeypatch):
    fired = []
    monkeypatch.setattr(config, "HUBS", [("https://h/ingest", "s")])
    monkeypatch.setattr(config, "SHIP_EXPEDITE", False)
    monkeypatch.setattr(expedite, "should_ship", lambda c: True)
    monkeypatch.setattr(ship, "expedite", lambda: fired.append(1))
    expedite.check(node_db)
    assert fired == []


def test_check_fires_on_elevated(node_db, monkeypatch):
    done = threading.Event()
    monkeypatch.setattr(config, "HUBS", [("https://h/ingest", "s")])
    monkeypatch.setattr(config, "SHIP_EXPEDITE", True)
    monkeypatch.setattr(expedite, "should_ship", lambda c: True)
    monkeypatch.setattr(ship, "expedite", lambda: (done.set(), 0)[1])
    expedite.check(node_db)
    assert done.wait(2.0)  # the daemon thread ran ship.expedite()
