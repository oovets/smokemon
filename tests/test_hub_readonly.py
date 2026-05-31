"""Hub read-only connection (item 7) + inventory surface (item 4b)."""

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
            ro.execute("INSERT INTO host_samples (ts, node) VALUES (1.0, 'x')")
    finally:
        ro.close()


def test_inventory_groups_latest_facts_per_node(hub_db):
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    schema.insert(conn, "device_facts",
                  [{"ts": 1.0, "key": "model", "value": "Pi 4", "kind": "hw"},
                   {"ts": 1.0, "key": "kernel", "value": "6.1", "kind": "os"},
                   {"ts": 5.0, "key": "kernel", "value": "6.2", "kind": "os"}],  # newer wins
                  node="pi-01")
    schema.insert(conn, "device_facts",
                  [{"ts": 2.0, "key": "model", "value": "Jetson", "kind": "hw"}], node="jetson-01")
    conn.commit()

    out = hubapi.inventory(conn, now=100.0)
    conn.close()
    by_node = {n["node"]: n for n in out["nodes"]}
    assert by_node["pi-01"]["facts"]["kernel"]["value"] == "6.2"   # latest per (node,key)
    assert by_node["pi-01"]["facts"]["model"]["kind"] == "hw"
    assert by_node["jetson-01"]["facts"]["model"]["value"] == "Jetson"
    assert by_node["pi-01"]["updated_s"] == 95                     # now - freshest fact ts (5.0)
