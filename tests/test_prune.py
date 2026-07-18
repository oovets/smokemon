"""Node DB retention/pruning: deletes shipped+old rows, keeps un-shipped.
Multi-hub: "shipped" = confirmed by AT LEAST ONE configured hub (MAX cursor across dests)."""

import time

from smokemon import prune, schema, ship


def _cursor(conn, dest, table, last_id):
    conn.execute("INSERT INTO ship_state(dest,table_name,last_id) VALUES(?,?,?)", (dest, table, last_id))
    conn.commit()


def test_prune_deletes_old_shipped(node_db):
    now = time.time()
    old, recent = now - 30 * 86400, now - 3600
    for ts in (old, old, recent):
        schema.insert(node_db, "heartbeats", [{"ts": ts, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)
    maxid = node_db.execute("SELECT MAX(id) FROM heartbeats").fetchone()[0]
    _cursor(node_db, "d1", "heartbeats", maxid)
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True, dests=["d1"])
    assert deleted.get("heartbeats") == 2
    assert node_db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1


def test_prune_keeps_unshipped(node_db):
    now = time.time()
    schema.insert(node_db, "heartbeats", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)  # no cursor row -> nothing shipped yet
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True, dests=["d1"])
    assert "heartbeats" not in deleted
    assert node_db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1


def test_prune_age_only_when_no_hub(node_db):
    now = time.time()
    schema.insert(node_db, "heartbeats", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=False)
    assert deleted.get("heartbeats") == 1
    assert node_db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 0


def test_prune_disabled_is_noop(node_db):
    schema.insert(node_db, "heartbeats", [{"ts": 1.0, "cpu_pct": 1.0}])
    node_db.commit()
    assert prune.prune(node_db, retention_days=0) == {}
    assert node_db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1


def test_prune_uses_max_cursor_across_dests(node_db):
    """Delete-when-one-confirmed: prune frees rows up to the HIGHEST cursor among configured hubs."""
    now = time.time()
    for _ in range(4):
        schema.insert(node_db, "heartbeats", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)
    _cursor(node_db, "d1", "heartbeats", 2)   # one hub behind
    _cursor(node_db, "d2", "heartbeats", 4)   # one hub ahead -> MAX = 4
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True, dests=["d1", "d2"])
    assert deleted.get("heartbeats") == 4
    assert node_db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 0


def test_prune_unreachable_hub_does_not_block(node_db):
    """A configured-but-never-reached hub (no cursor row -> 0) must not hold back data another hub took."""
    now = time.time()
    for _ in range(3):
        schema.insert(node_db, "heartbeats", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)
    _cursor(node_db, "alive", "heartbeats", 3)
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True, dests=["alive", "dead"])
    assert deleted.get("heartbeats") == 3


def test_prune_all_cursors_zero_deletes_nothing(node_db):
    now = time.time()
    schema.insert(node_db, "heartbeats", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)  # hubs configured but nothing shipped -> MAX = 0
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True, dests=["d1", "d2"])
    assert "heartbeats" not in deleted
    assert node_db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1
