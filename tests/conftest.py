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
def hub_conn(hub_db):
    """An initialised hub-schema SQLite connection on an isolated DB; closed on teardown."""
    from smokemon import core, schema
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def seed(hub_conn):
    """Writers for the hub-side rows the incident views read.

    The hub never sees the node's incident_state table -- only the append-only transition rows --
    so these write transitions directly rather than driving the detector. That is exactly what
    arrives over the wire, and it lets a test express "open incident whose close never arrived",
    which is the case the read layer is most obliged to get right."""
    from types import SimpleNamespace

    from smokemon import schema

    def heartbeat(node, ts, interval_s=300.0, **extra):
        schema.insert(hub_conn, "heartbeats",
                      [{"ts": ts, "interval_s": interval_s, **extra}], node=node)
        hub_conn.commit()

    def incident(node, uid, *, signal="ping.loss", entity="1.1.1.1", severity="warn",
                 opened_ts=1_000_000.0, closed_ts=None, worst_value=None):
        """One open transition, plus a close when closed_ts is given. Terminal rows carry
        severity 'info' the way the node writes them (they report the end, not the fault)."""
        rows = [{"ts": opened_ts, "uid": uid, "transition": "open", "signal": signal,
                 "entity": entity, "severity": severity, "worst_value": worst_value,
                 "opened_ts": opened_ts}]
        if closed_ts is not None:
            rows.append({"ts": closed_ts, "uid": uid, "transition": "close", "signal": signal,
                         "entity": entity, "severity": "info", "opened_ts": opened_ts,
                         "duration_s": closed_ts - opened_ts})
        schema.insert(hub_conn, "incidents", rows, node=node)
        hub_conn.commit()

    def event(node, ts, severity="info", source="probe", ev="thing", detail=""):
        schema.insert(hub_conn, "ext_events",
                      [{"ts": ts, "source": source, "severity": severity,
                        "event": ev, "detail": detail}], node=node)
        hub_conn.commit()

    return SimpleNamespace(conn=hub_conn, heartbeat=heartbeat, incident=incident, event=event)


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
