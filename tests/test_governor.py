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
    over, reason = governor.should_shed("inventory")
    assert over and "rss" in reason


def test_note_is_throttled(node_db):
    governor._last_note = 0.0
    governor.note(node_db, "mtr", "rss 99>50MB")
    first = node_db.execute("SELECT COUNT(*) FROM ext_events").fetchone()[0]
    governor.note(node_db, "mtr", "rss 99>50MB")  # within throttle window
    second = node_db.execute("SELECT COUNT(*) FROM ext_events").fetchone()[0]
    assert first == 1 and second == 1


def test_sheddable_probes_are_all_actually_registered(monkeypatch):
    """EXPENSIVE named three probes that had been deleted, which left the governor unable to
    shed anything at all -- 'degrade gracefully under pressure' with no mechanism behind it.
    A name that no longer exists must fail here rather than silently disable the back-off.

    The optional probes are enabled first: they are conditionally registered, and comparing
    against a default config would let a typo in EXPENSIVE pass whenever the probe it names
    happens to be switched off."""
    from smokemon import collect, config, governor

    monkeypatch.setattr(config, "LOGEXCERPT_ENABLED", True)
    monkeypatch.setattr(config, "LOGEXCERPT_PATHS", ["/var/log/syslog"])
    monkeypatch.setattr(config, "INVENTORY_ENABLED", True)
    registered = {name for _interval, name, _fn in collect._probes("all")}
    missing = set(governor.EXPENSIVE) - registered
    assert not missing, f"governor would try to shed probes that do not run: {missing}"


def test_detection_is_never_sheddable():
    """A node under memory pressure is exactly when something is wrong. Shedding the probes
    that would notice, to save footprint, trades away the reason the agent exists."""
    from smokemon import governor

    for core_probe in ("ping", "net", "host", "heartbeat", "sweep"):
        assert core_probe not in governor.EXPENSIVE
