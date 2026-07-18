"""Schema migration must be additive: ALTER ADD missing body columns on legacy DBs
without touching existing rows, and create any new tables."""

import sqlite3

import pytest

from smokemon import core, schema


def test_init_on_empty_db_creates_all_tables(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in schema.STD_TABLES:
        assert t in tables, f"missing table: {t}"
    conn.close()


def test_pragmas_applied(tmp_db):
    """Only the three RSS-free PRAGMAs (WAL, NORMAL sync, busy_timeout) should be
    set by core.connect. Anything more would bloat node footprint and skew the
    self-RSS metric host.py reports. See core.py docstring."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    conn.close()


def test_migration_adds_new_columns_on_legacy_tables(tmp_path):
    """Simulate a DB written before the current body columns and verify they get ALTERed in."""
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.executescript("""
        CREATE TABLE heartbeats (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, interval_s REAL, uptime_s REAL, node TEXT
        );
        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, uid TEXT NOT NULL,
            transition TEXT NOT NULL, signal TEXT NOT NULL, node TEXT
        );
        INSERT INTO heartbeats (ts, interval_s, node) VALUES (100.0, 300.0, 'legacy');
        INSERT INTO incidents (ts, uid, transition, signal, node)
            VALUES (100.0, 'u1', 'open', 'rtt', 'legacy');
    """)
    # Stamp the current schema version: core.SCHEMA_VERSION is a tripwire that moves an
    # older DB aside wholesale, so a file that reaches ensure_body_columns is by definition
    # already at the current version and only missing individual columns.
    raw.execute(f"PRAGMA user_version={core.SCHEMA_VERSION}")
    raw.commit()
    raw.close()

    conn = core.connect(str(legacy))
    schema.init_node(conn)

    hb_cols = {r[1] for r in conn.execute("PRAGMA table_info(heartbeats)")}
    for new in ("agent_uptime_s", "db_mb", "wal_mb", "disk_free_gb", "wear_pct",
                "open_incidents", "signal_drops", "ver"):
        assert new in hb_cols, f"heartbeats missing {new} after migration"

    inc_cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
    for new in ("severity", "worst_value", "opened_ts", "duration_s", "rule_hash"):
        assert new in inc_cols, f"incidents missing {new} after migration"

    # legacy rows preserved
    assert conn.execute("SELECT ts, interval_s, node FROM heartbeats").fetchone() == (100.0, 300.0, "legacy")
    assert conn.execute("SELECT uid, transition FROM incidents").fetchone() == ("u1", "open")

    conn.close()


def test_safe_add_column_tolerates_duplicate(tmp_db):
    """The guard that prevents the upgrade-race crash: adding an already-present column
    returns False instead of raising OperationalError('duplicate column name')."""
    conn = core.connect(str(tmp_db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    assert schema._safe_add_column(conn, "t", "b REAL") is True
    assert schema._safe_add_column(conn, "t", "b REAL") is False  # must not raise
    # A genuinely malformed ALTER must still surface.
    with pytest.raises(sqlite3.OperationalError):
        schema._safe_add_column(conn, "t", "not valid ddl !!!")
    conn.close()


def test_concurrent_migration_no_duplicate_crash(tmp_path):
    """Two daemons open the same legacy node DB and both run the migration at startup. The
    loser of the ALTER race must not crash. Simulated with two connections: c1 adds + commits
    a new column, c2 (which decided to add the same column from its own stale table_info read)
    then adds it and gets a safe no-op."""
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.executescript(
        "CREATE TABLE heartbeats (id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
        "interval_s REAL, node TEXT)"
    )
    raw.execute(f"PRAGMA user_version={core.SCHEMA_VERSION}")  # see the migration test above
    raw.commit()
    raw.close()

    c1 = core.connect(str(legacy))
    c2 = core.connect(str(legacy))
    assert schema._safe_add_column(c1, "heartbeats", "db_mb REAL") is True
    c1.commit()
    # c2 lost the race for db_mb; the guard turns the duplicate into a no-op.
    assert schema._safe_add_column(c2, "heartbeats", "db_mb REAL") is False

    # Full migration from both connections must converge without raising.
    schema.ensure_body_columns(c1)
    schema.ensure_body_columns(c2)
    cols = {r[1] for r in c2.execute("PRAGMA table_info(heartbeats)")}
    assert {"db_mb", "wal_mb", "agent_uptime_s"} <= cols
    c1.close()
    c2.close()


def test_migration_is_idempotent(tmp_db):
    """Running init twice on the same DB must not change column count."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    cols1 = sorted(r[1] for r in conn.execute("PRAGMA table_info(heartbeats)"))
    schema.init_node(conn)
    cols2 = sorted(r[1] for r in conn.execute("PRAGMA table_info(heartbeats)"))
    assert cols1 == cols2
    conn.close()
