"""End-to-end checks on the incident pivot's central claims.

Unit tests cover the state machine and the persistence rules separately. These drive the whole
path -- probe sample in, rows on disk, batch on the wire, incident reconstructed hub-side --
because the properties that matter most only exist at the seams.
"""

import pytest

from smokemon import baseline, core, detect, heartbeat, incidents, query, schema, ship, signals


@pytest.fixture(autouse=True)
def _clean():
    signals.reset()
    baseline.reset()
    detect.reset()
    detect.reload_rules("")
    yield
    signals.reset()
    baseline.reset()
    detect.reset()


@pytest.fixture()
def node(tmp_path):
    c = core.connect(str(tmp_path / "node.db"))
    schema.init_node(c)
    incidents.ensure_table(c)
    baseline.ensure_table(c)
    ship.init_state(c)
    yield c
    c.close()


@pytest.fixture()
def hub(tmp_path):
    c = core.connect(str(tmp_path / "hub.db"))
    schema.init_hub(c)
    yield c
    c.close()


def _drive(conn, values, signal="ping.loss", entity="1.1.1.1", t0=1_000_000.0, step=10.0):
    """Feed a value sequence, persisting whatever the detector decides."""
    wall, mono = t0, 5_000.0
    for v in values:
        wall += step
        mono += step
        incidents.apply(conn, detect.evaluate(signal, entity, v, wall, mono))
    return wall


def _push(node_conn, hub_conn, node_name="pi01"):
    """Ship every pending row from node to hub, the way the real shipper would: table by
    table, each with its own cursor, and the hub keying on (node, src_id)."""
    payload, maxids = ship.gather(node_conn, "hub")
    for table, t in payload.items():
        cols = t["columns"]
        body = [c for c in cols if c not in ("id", "node")]
        idx = [cols.index(c) for c in body]
        src = cols.index("id")
        hub_conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(body)},node,src_id) "
            f"VALUES ({','.join('?' * len(body))},?,?)",
            [[r[i] for i in idx] + [node_name, r[src]] for r in t["rows"]])
    hub_conn.commit()
    for table, mid in maxids.items():
        ship._set_last(node_conn, "hub", table, mid)
    node_conn.commit()
    return payload


# ---------- the headline claim ----------

def test_a_healthy_hour_writes_only_heartbeats(node):
    """The pivot's whole justification. If normal operation ever writes sample rows again,
    the design has stopped delivering what it cost."""
    _drive(node, [0.0] * 360)                      # an hour of healthy 10s ping runs
    for _ in range(12):                            # twelve 5-minute heartbeats
        heartbeat.collect(node)

    counts = {t: node.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in schema.STD_TABLES}
    assert counts["heartbeats"] == 12
    assert counts["incidents"] == 0
    assert counts["incident_samples"] == 0
    assert counts["ext_events"] == 0
    assert sum(v for k, v in counts.items() if k != "heartbeats") == 0, counts


def test_heartbeat_reports_the_signal_registry(node):
    """A node whose detector went quiet because a probe stopped feeding looks identical to a
    healthy one without this."""
    _drive(node, [0.0] * 5)
    heartbeat.collect(node)
    row = node.execute("SELECT signals, signal_kb, open_incidents, agent_uptime_s, ver "
                       "FROM heartbeats").fetchone()
    n_signals, kb, open_inc, uptime, ver = row
    assert n_signals == 1 and kb > 0
    assert open_inc == 0 and uptime is not None and ver


# ---------- node -> hub ----------

def test_incident_reconstructs_on_the_hub(node, hub):
    end = _drive(node, [0.0] * 8 + [50.0] * 6 + [0.0] * 12)   # open then close
    _push(node, hub)

    got = query.load_incidents(hub, 0, end + 1000)
    assert len(got) == 1
    inc = got[0]
    assert inc["signal"] == "ping.loss" and inc["entity"] == "1.1.1.1"
    assert inc["state"] == "closed"
    assert inc["severity"] == "error", "terminal rows carry info; the incident must not look harmless"
    assert inc["worst_value"] == 50.0
    assert inc["duration_s"] and inc["duration_s"] > 0

    samples = query.load_incident_samples(hub, inc["uid"])
    phases = {s["phase"] for s in samples}
    assert {"pre", "during"} <= phases
    assert all(s["ts"] for s in samples)


def test_ongoing_incident_is_not_reported_as_closed(node, hub):
    """The hub must never infer 'recovered' from the absence of a close row -- the node could
    simply have died mid-incident."""
    _drive(node, [0.0] * 8 + [50.0] * 6)
    _push(node, hub)
    got = query.load_incidents(hub, 0, 2_000_000)
    assert len(got) == 1 and got[0]["state"] == "ongoing"


def test_samples_arriving_before_their_parent_are_not_lost(node, hub):
    """uid is a content key, not a foreign key. Ship order is a latency optimisation; a child
    that lands first must be an unjoined-but-valid row that completes when the parent arrives.
    The old ping_rtts design dropped such rows silently."""
    end = _drive(node, [0.0] * 8 + [50.0] * 6 + [0.0] * 12)
    payload, _ = ship.gather(node, "hub")

    # Deliver ONLY the samples first.
    t = payload["incident_samples"]
    cols = t["columns"]
    body = [c for c in cols if c not in ("id", "node")]
    idx = [cols.index(c) for c in body]
    hub.executemany(
        f"INSERT OR IGNORE INTO incident_samples ({','.join(body)},node,src_id) "
        f"VALUES ({','.join('?' * len(body))},?,?)",
        [[r[i] for i in idx] + ["pi01", r[cols.index("id")]] for r in t["rows"]])
    hub.commit()

    n_orphans, oldest = query.orphan_stats(hub)
    assert n_orphans > 0, "orphaned samples were dropped instead of held"
    assert oldest >= 0

    _push(node, hub)                       # the parent finally arrives
    assert query.orphan_stats(hub)[0] == 0
    inc = query.load_incidents(hub, 0, end + 1000)[0]
    assert len(query.load_incident_samples(hub, inc["uid"])) > 0


def test_redelivering_the_same_batch_changes_nothing(node, hub):
    """Invariant 14: the same shipment may safely be delivered more than once."""
    end = _drive(node, [0.0] * 8 + [50.0] * 6 + [0.0] * 12)
    payload = _push(node, hub)
    before = query.load_incidents(hub, 0, end + 1000)

    # Re-deliver the identical payload without advancing any cursor.
    for table, t in payload.items():
        cols = t["columns"]
        body = [c for c in cols if c not in ("id", "node")]
        idx = [cols.index(c) for c in body]
        hub.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(body)},node,src_id) "
            f"VALUES ({','.join('?' * len(body))},?,?)",
            [[r[i] for i in idx] + ["pi01", r[cols.index("id")]] for r in t["rows"]])
    hub.commit()

    assert query.load_incidents(hub, 0, end + 1000) == before


def test_incidents_ship_before_their_evidence(node):
    """Ordering is only a latency optimisation, but it should still hold: under a backlog the
    'what broke' rows must ride the earliest batch."""
    order = list(ship._ordered_tables())
    assert order.index("incidents") < order.index("incident_samples")
    assert order.index("incident_samples") < order.index("heartbeats")


# ---------- schema tripwire ----------

def test_stale_database_is_set_aside_not_deleted(tmp_path):
    """The rows are worthless under the new model, but a tool for explaining incidents must
    not contain a path that silently removes a database."""
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE ping_runs (id INTEGER PRIMARY KEY, ts REAL)")
    old.execute("INSERT INTO ping_runs (ts) VALUES (1.0)")
    old.execute("PRAGMA user_version=1")
    old.commit()
    old.close()

    conn = core.connect(str(path))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ping_runs" not in names, "stale database was reused"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION
    finally:
        conn.close()

    aside = tmp_path / "old.db.old-v1"
    assert aside.exists(), "the previous database was deleted rather than set aside"
    kept = sqlite3.connect(str(aside))
    assert kept.execute("SELECT COUNT(*) FROM ping_runs").fetchone()[0] == 1
    kept.close()


def test_current_database_is_left_alone(tmp_path):
    path = tmp_path / "cur.db"
    c = core.connect(str(path))
    schema.init_node(c)
    schema.insert(c, "heartbeats", [{"ts": 1.0, "interval_s": 300.0}])
    c.commit()
    c.close()

    c2 = core.connect(str(path))
    try:
        assert c2.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1
    finally:
        c2.close()
    assert not list(tmp_path.glob("*.old-v*")), "a current database was set aside"


def test_write_budget_does_not_regress(tmp_path):
    """The write budget is the pivot's whole justification, so it gets a test rather than a
    README claim. Bounds are generous -- this catches a probe that started writing again or a
    commit-per-sample regression, not a few bytes of drift."""
    from scripts.bench_write_budget import run

    r = run(hours=6.0, ping_interval=10.0, host_interval=30.0,
            hb_interval=300.0, incidents_per_day=2.0)
    per_day = 4.0

    hb = r["rows"]["heartbeats"]
    assert hb == 72, f"expected 72 heartbeats in 6h, got {hb}"
    assert r["commits"] * per_day < 600, \
        f"{r['commits'] * per_day:.0f} commits/day -- something is committing per sample"
    assert r["appended"] * per_day < 12e6, \
        f"{r['appended'] * per_day / 1e6:.1f} MB/day appended -- write budget regressed"
    # The tables that must stay empty while nothing is wrong.
    assert r["rows"]["device_facts"] == 0
    assert r["rows"]["log_excerpts"] == 0
