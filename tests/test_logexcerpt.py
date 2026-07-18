"""Event-driven log excerpts: off by default, tails only on incident, with offset cursor,
drop-oldest byte cap and secret redaction."""

from smokemon import config, schema
from smokemon.probes import logexcerpt


def _reset():
    logexcerpt._last_event_id = None


def _enable(monkeypatch, path, max_bytes=16 * 1024, always=False):
    monkeypatch.setattr(config, "LOGEXCERPT_ENABLED", True)
    monkeypatch.setattr(config, "LOGEXCERPT_PATHS", [str(path)])
    monkeypatch.setattr(config, "LOGEXCERPT_MAX_BYTES", max_bytes)
    monkeypatch.setattr(config, "LOGEXCERPT_ALWAYS", always)


def _event(conn, severity="warn", source="governor", event="shed"):
    schema.insert(conn, "ext_events", [{"ts": 1.0, "source": source, "severity": severity,
                                        "event": event, "detail": "x"}])
    conn.commit()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM log_excerpts").fetchone()[0]


def test_disabled_by_default(node_db, tmp_path, monkeypatch):
    _reset()
    log = tmp_path / "app.log"; log.write_text("line\n")
    # config.LOGEXCERPT_ENABLED is False by default
    logexcerpt.collect(node_db)
    assert "log_excerpts" in schema.STD_TABLES
    assert _count(node_db) == 0


def test_no_dump_on_first_run_then_tail_on_event(node_db, tmp_path, monkeypatch):
    _reset()
    log = tmp_path / "app.log"; log.write_text("OLD HISTORY\n" * 100)
    _enable(monkeypatch, log)

    logexcerpt.collect(node_db)            # first run: seeds cursors + event mark, no capture
    assert _count(node_db) == 0

    log.write_text("OLD HISTORY\n" * 100 + "fresh incident line\n")
    _event(node_db)                         # an elevated event lands
    logexcerpt.collect(node_db)
    assert _count(node_db) == 1
    excerpt = node_db.execute("SELECT excerpt FROM log_excerpts").fetchone()[0]
    assert "fresh incident line" in excerpt
    assert "OLD HISTORY" not in excerpt     # only bytes written after we started watching


def test_cursor_prevents_resend(node_db, tmp_path, monkeypatch):
    _reset()
    log = tmp_path / "app.log"; log.write_text("")
    _enable(monkeypatch, log, always=True)  # capture every cycle

    logexcerpt.collect(node_db)              # seed cursor at EOF (0, empty file)
    log.write_text("first\n")
    logexcerpt.collect(node_db)
    log.write_text("first\nsecond\n")
    logexcerpt.collect(node_db)
    excerpts = [r[0] for r in node_db.execute("SELECT excerpt FROM log_excerpts ORDER BY id")]
    assert "first" in excerpts[0]
    assert excerpts[1] == "second\n"         # the cursor skipped already-sent bytes


def test_byte_cap_drops_oldest(node_db, tmp_path, monkeypatch):
    _reset()
    log = tmp_path / "app.log"; log.write_text("")
    _enable(monkeypatch, log, max_bytes=64, always=True)
    logexcerpt.collect(node_db)              # seed cursor at 0 (empty file)
    log.write_text("X" * 500)                # 500 new bytes, cap is 64
    logexcerpt.collect(node_db)
    row = node_db.execute("SELECT bytes, dropped FROM log_excerpts ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] <= 64                      # excerpt no larger than the cap
    assert row[1] == 500 - 64                # the rest was dropped (oldest first)


def test_secret_is_redacted(node_db, tmp_path, monkeypatch):
    _reset()
    monkeypatch.setattr(config, "HUB_SECRET", "supersecretvalue")
    log = tmp_path / "app.log"; log.write_text("")
    _enable(monkeypatch, log, always=True)
    logexcerpt.collect(node_db)              # seed
    log.write_text("Authorization: Bearer abc123tok\nkey=supersecretvalue ok\n")
    logexcerpt.collect(node_db)
    excerpt = node_db.execute("SELECT excerpt FROM log_excerpts ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "abc123tok" not in excerpt
    assert "supersecretvalue" not in excerpt
    assert "***" in excerpt


def test_info_events_do_not_trigger(node_db, tmp_path, monkeypatch):
    _reset()
    log = tmp_path / "app.log"; log.write_text("")
    _enable(monkeypatch, log)
    logexcerpt.collect(node_db)              # seed
    log.write_text("noise\n")
    _event(node_db, severity="info")         # routine event -> no capture
    logexcerpt.collect(node_db)
    assert _count(node_db) == 0
