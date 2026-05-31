"""Footprint governor: sheds expensive probes only when a budget is breached, events throttled."""

from smokemon import config, governor


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "MAX_RSS_MB", 0.0)
    monkeypatch.setattr(config, "MAX_DB_MB", 0.0)
    assert governor.over_budget() == (False, "")
    assert governor.should_shed("mtr")[0] is False


def test_non_expensive_never_shed(monkeypatch):
    monkeypatch.setattr(config, "MAX_RSS_MB", 0.001)
    monkeypatch.setattr(governor, "rss_mb", lambda: 9999.0)
    assert governor.should_shed("ping")[0] is False
    assert governor.should_shed("host")[0] is False


def test_shed_when_over_rss(monkeypatch):
    monkeypatch.setattr(config, "MAX_RSS_MB", 50.0)
    monkeypatch.setattr(config, "MAX_DB_MB", 0.0)
    monkeypatch.setattr(governor, "rss_mb", lambda: 9999.0)
    over, reason = governor.should_shed("mtr")
    assert over and "rss" in reason


def test_note_is_throttled(node_db):
    governor._last_note = 0.0
    governor.note(node_db, "mtr", "rss 99>50MB")
    first = node_db.execute("SELECT COUNT(*) FROM ext_events").fetchone()[0]
    governor.note(node_db, "mtr", "rss 99>50MB")  # within throttle window
    second = node_db.execute("SELECT COUNT(*) FROM ext_events").fetchone()[0]
    assert first == 1 and second == 1
