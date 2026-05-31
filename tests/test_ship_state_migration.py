"""ship_state migrates from the old single-cursor schema (table_name PK) to per-destination
(dest, table_name), idempotently, and forgets cursors for hubs no longer configured."""

from smokemon import config, core, ship


def _cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(ship_state)").fetchall()]


def test_fresh_db_gets_composite_schema(tmp_db):
    conn = core.connect(str(tmp_db))
    ship.init_state(conn)
    assert "dest" in _cols(conn)
    conn.close()


def test_migration_from_old_schema_remaps_to_primary(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    conn.execute("CREATE TABLE ship_state (table_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO ship_state(table_name,last_id) VALUES('host_samples', 42)")
    conn.commit()
    monkeypatch.setattr(config, "HUBS", [("https://a/ingest", "s")])
    ship.init_state(conn)
    assert "dest" in _cols(conn)
    dest = config.hub_dest("https://a/ingest")
    assert conn.execute("SELECT dest,last_id FROM ship_state WHERE table_name='host_samples'").fetchone() \
        == (dest, 42)
    conn.close()


def test_init_state_idempotent(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    monkeypatch.setattr(config, "HUBS", [("https://a/ingest", "s")])
    ship.init_state(conn)
    ship._set_last(conn, config.hub_dest("https://a/ingest"), "host_samples", 7)
    conn.commit()
    ship.init_state(conn)  # second call must not error or lose the cursor
    assert "dest" in _cols(conn)
    assert ship._last(conn, config.hub_dest("https://a/ingest"), "host_samples") == 7
    conn.close()


def test_orphan_cursor_cleanup_when_hubs_configured(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    monkeypatch.setattr(config, "HUBS", [("https://a/ingest", "s")])
    ship.init_state(conn)
    keep = config.hub_dest("https://a/ingest")
    ship._set_last(conn, keep, "host_samples", 5)
    ship._set_last(conn, "h_orphan", "host_samples", 9)  # a dest no longer configured
    conn.commit()
    ship.init_state(conn)  # re-run -> orphan dropped, configured kept
    assert [r[0] for r in conn.execute("SELECT DISTINCT dest FROM ship_state").fetchall()] == [keep]
    conn.close()


def test_no_orphan_cleanup_without_hubs(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    monkeypatch.setattr(config, "HUBS", [])
    ship.init_state(conn)
    ship._set_last(conn, "h_x", "host_samples", 5)
    conn.commit()
    ship.init_state(conn)  # no hubs configured -> cursors preserved (nothing to ship, nothing to forget)
    assert conn.execute("SELECT COUNT(*) FROM ship_state").fetchone()[0] == 1
    conn.close()
