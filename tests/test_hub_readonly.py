"""Hub read-only connection + the inventory surface.

The dashboard reads through a separate read-only connection so a bug in a read path cannot
corrupt the hub DB the nodes are writing into, and so GETs run under WAL beside ingest.
"""

import sqlite3

import pytest

from smokemon import core, hubapi, schema


def test_connect_ro_rejects_writes(hub_db):
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    conn.close()
    ro = core.connect_ro(str(hub_db))
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO heartbeats (ts, node) VALUES (1.0, 'x')")
    finally:
        ro.close()


def test_read_api_works_through_the_read_only_connection(hub_db, seed):
    """The read layer must not need write access -- no implicit temp tables, no PRAGMA writes."""
    seed.heartbeat("n1", 1_000_000.0 - 10)
    seed.incident("n1", "u1", opened_ts=1_000_000.0 - 100)
    ro = core.connect_ro(str(hub_db))
    try:
        assert [r["node"] for r in hubapi.fleet(ro, 1_000_000.0)] == ["n1"]
        assert hubapi.incidents_feed(ro, hours=24, now=1_000_000.0)["counts"]["total"] == 1
        assert "smokemon_node_live" in hubapi.prometheus(ro, 1_000_000.0)
    finally:
        ro.close()


def test_inventory_keeps_the_latest_value_per_node_and_key(hub_conn):
    schema.insert(hub_conn, "device_facts",
                  [{"ts": 1.0, "key": "model", "value": "Pi 4", "kind": "hw"},
                   {"ts": 1.0, "key": "kernel", "value": "6.1", "kind": "os"},
                   {"ts": 5.0, "key": "kernel", "value": "6.2", "kind": "os"}],  # newer wins
                  node="pi-01")
    schema.insert(hub_conn, "device_facts",
                  [{"ts": 2.0, "key": "model", "value": "Jetson", "kind": "hw"}], node="jetson-01")
    hub_conn.commit()

    nodes = hubapi.inventory(hub_conn, now=100.0)["nodes"]
    assert nodes["pi-01"]["kernel"]["value"] == "6.2"
    assert nodes["pi-01"]["kernel"]["ts"] == 5.0
    assert nodes["pi-01"]["model"]["kind"] == "hw"
    assert nodes["jetson-01"]["model"]["value"] == "Jetson"


def test_inventory_of_an_empty_hub(hub_conn):
    assert hubapi.inventory(hub_conn, now=100.0) == {"nodes": {}}
