"""Shared pytest fixtures for smokemon tests."""

import time

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite DB path + node identity. Re-imports smokemon.config so the
    module-level NODE/DB_PATH constants pick up the patched env-vars."""
    db = tmp_path / "node.db"
    monkeypatch.setenv("SMOKEMON_DB", str(db))
    monkeypatch.setenv("SMOKEMON_NODE", "testnode")
    import importlib

    import smokemon.config
    importlib.reload(smokemon.config)
    return db


@pytest.fixture
def hub_db(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"
    monkeypatch.setenv("SMOKEMON_HUB_DB", str(db))
    monkeypatch.setenv("SMOKEMON_NODE", "testnode")
    import importlib

    import smokemon.config
    importlib.reload(smokemon.config)
    return db


@pytest.fixture
def ts0():
    """A stable timestamp slightly in the past so test windows include it."""
    return time.time() - 600


@pytest.fixture
def node_db(tmp_db):
    """An initialised node-schema SQLite connection on an isolated DB; closed on teardown."""
    from smokemon import core, schema
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    try:
        yield conn
    finally:
        conn.close()
