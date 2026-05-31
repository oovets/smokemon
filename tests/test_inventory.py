"""Inventory probe: emits facts once, stays silent until a value changes, seeds from DB."""

from smokemon import schema
from smokemon.probes import inventory


def _reset():
    inventory._loaded = False
    inventory._last = {}


def test_emits_then_delta(node_db, monkeypatch):
    _reset()
    monkeypatch.setattr(inventory, "_gather", lambda: [("model", "Pi", "hw"), ("kernel", "6.1", "os")])
    inventory.collect(node_db)
    assert node_db.execute("SELECT COUNT(*) FROM device_facts").fetchone()[0] == 2

    inventory.collect(node_db)  # nothing changed -> no new rows
    assert node_db.execute("SELECT COUNT(*) FROM device_facts").fetchone()[0] == 2

    monkeypatch.setattr(inventory, "_gather", lambda: [("model", "Pi", "hw"), ("kernel", "6.2", "os")])
    inventory.collect(node_db)  # one fact changed -> one new row
    assert node_db.execute("SELECT COUNT(*) FROM device_facts").fetchone()[0] == 3


def test_seed_from_db_avoids_reemit(node_db, monkeypatch):
    schema.insert(node_db, "device_facts", [{"ts": 1.0, "key": "model", "value": "Pi", "kind": "hw"}])
    node_db.commit()
    _reset()
    monkeypatch.setattr(inventory, "_gather", lambda: [("model", "Pi", "hw")])
    inventory.collect(node_db)  # unchanged vs DB -> no new row
    assert node_db.execute("SELECT COUNT(*) FROM device_facts").fetchone()[0] == 1
