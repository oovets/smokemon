"""Node DB retention/pruning: deletes shipped+old rows, keeps un-shipped, cleans rtt orphans."""

import time

from smokemon import prune, schema, ship


def test_prune_deletes_old_shipped(node_db):
    now = time.time()
    old, recent = now - 30 * 86400, now - 3600
    for ts in (old, old, recent):
        schema.insert(node_db, "host_samples", [{"ts": ts, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)
    maxid = node_db.execute("SELECT MAX(id) FROM host_samples").fetchone()[0]
    node_db.execute("INSERT INTO ship_state(table_name,last_id) VALUES('host_samples',?)", (maxid,))
    node_db.commit()
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True)
    assert deleted.get("host_samples") == 2
    assert node_db.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0] == 1


def test_prune_keeps_unshipped(node_db):
    now = time.time()
    schema.insert(node_db, "host_samples", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    ship.init_state(node_db)  # cursor at 0 -> nothing shipped yet
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=True)
    assert "host_samples" not in deleted
    assert node_db.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0] == 1


def test_prune_age_only_when_no_hub(node_db):
    now = time.time()
    schema.insert(node_db, "host_samples", [{"ts": now - 30 * 86400, "cpu_pct": 1.0}])
    node_db.commit()
    deleted = prune.prune(node_db, now=now, retention_days=14, require_shipped=False)
    assert deleted.get("host_samples") == 1
    assert node_db.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0] == 0


def test_prune_disabled_is_noop(node_db):
    schema.insert(node_db, "host_samples", [{"ts": 1.0, "cpu_pct": 1.0}])
    node_db.commit()
    assert prune.prune(node_db, retention_days=0) == {}
    assert node_db.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0] == 1


def test_prune_cleans_rtt_orphans(node_db):
    now = time.time()
    rid_old = schema.insert_one(node_db, "ping_runs", {"ts": now - 30 * 86400, "target": "1.1.1.1"})
    rid_new = schema.insert_one(node_db, "ping_runs", {"ts": now - 100, "target": "1.1.1.1"})
    node_db.executemany("INSERT INTO ping_rtts(run_id,rtt_ms) VALUES(?,?)",
                        [(rid_old, 1.0), (rid_old, 1.1), (rid_new, 2.0)])
    node_db.commit()
    prune.prune(node_db, now=now, retention_days=14, require_shipped=False)
    rtts = node_db.execute("SELECT DISTINCT run_id FROM ping_rtts").fetchall()
    assert rtts == [(rid_new,)]
