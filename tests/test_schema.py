"""Schema migration must be additive: ALTER ADD missing body columns on legacy DBs
without touching existing rows, and create any new tables."""

import sqlite3

from smokemon import core, schema


def test_init_on_empty_db_creates_all_tables(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in schema.STD_TABLES:
        assert t in tables, f"missing table: {t}"
    assert "ping_rtts" in tables
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
    """Simulate a v0.10 DB and verify v0.11 columns get ALTERed in."""
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.executescript("""
        CREATE TABLE host_samples (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            cpu_pct REAL, load1 REAL, load5 REAL, load15 REAL,
            mem_used_pct REAL, mem_total_mb REAL, temp_c REAL,
            disk_read_mbps REAL, disk_write_mbps REAL, node TEXT
        );
        CREATE TABLE wifi_samples (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, ssid TEXT, channel TEXT,
            phy_mode TEXT, rssi_dbm INTEGER, noise_dbm INTEGER,
            tx_rate_mbps REAL, node TEXT
        );
        CREATE TABLE disk_samples (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, mount TEXT NOT NULL,
            used_pct REAL, free_gb REAL, node TEXT
        );
        INSERT INTO host_samples (ts, cpu_pct, node) VALUES (100.0, 5.5, 'legacy');
        INSERT INTO wifi_samples (ts, ssid, rssi_dbm, node)
            VALUES (100.0, 'old', -55, 'legacy');
    """)
    raw.commit()
    raw.close()

    conn = core.connect(str(legacy))
    schema.init_node(conn)

    host_cols = {r[1] for r in conn.execute("PRAGMA table_info(host_samples)")}
    for new in ("swap_used_pct", "cache_mb", "oom_kill_count", "psi_cpu",
                "psi_mem", "psi_io", "cpu_freq_mhz", "cpu_throttle_count", "pi_throttle_bits"):
        assert new in host_cols, f"host_samples missing {new} after migration"

    wifi_cols = {r[1] for r in conn.execute("PRAGMA table_info(wifi_samples)")}
    for new in ("bssid", "retry_count", "discard_count", "beacon_loss"):
        assert new in wifi_cols, f"wifi_samples missing {new} after migration"

    disk_cols = {r[1] for r in conn.execute("PRAGMA table_info(disk_samples)")}
    assert "inode_used_pct" in disk_cols

    # legacy rows preserved
    host_row = conn.execute("SELECT ts, cpu_pct, node FROM host_samples").fetchone()
    assert host_row == (100.0, 5.5, "legacy")
    wifi_row = conn.execute("SELECT ts, ssid, rssi_dbm FROM wifi_samples").fetchone()
    assert wifi_row == (100.0, "old", -55)

    conn.close()


def test_migration_is_idempotent(tmp_db):
    """Running init twice on the same DB must not change column count."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    cols1 = sorted(r[1] for r in conn.execute("PRAGMA table_info(host_samples)"))
    schema.init_node(conn)
    cols2 = sorted(r[1] for r in conn.execute("PRAGMA table_info(host_samples)"))
    assert cols1 == cols2
    conn.close()
