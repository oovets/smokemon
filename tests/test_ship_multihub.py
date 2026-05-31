"""Multi-hub fan-out: each hub gets a full copy, compress-once at a shared frontier, and a
failed hub is isolated (cursor untouched, others unaffected) and catches up once it recovers.

Fan-out logic is exercised at the drain() level with a stubbed _post_body that records the
bytes delivered per URL - fast and deterministic, and it sidesteps the hub module's global
_conn (which makes running two real hubs in one process awkward)."""

import time

import pytest

from smokemon import config, core, schema, ship

HUBS = [("https://a/ingest", "s"), ("https://b/ingest", "s")]


@pytest.fixture
def node_with_rows(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    ship.init_state(conn)
    for i in range(3):
        schema.insert(conn, "host_samples", [{"ts": time.time(), "cpu_pct": float(i)}])
    conn.commit()
    yield conn
    conn.close()


def _stub_ok(monkeypatch):
    """All POSTs succeed; record delivered bodies per URL."""
    sink: dict[str, list] = {}
    monkeypatch.setattr(ship, "_post_body",
                        lambda url, secret, body: (sink.setdefault(url, []).append(body) or True))
    return sink


def _maxid(conn):
    return conn.execute("SELECT MAX(id) FROM host_samples").fetchone()[0]


def test_fanout_delivers_full_stream_to_each_hub(node_with_rows, monkeypatch):
    sink = _stub_ok(monkeypatch)
    ship.drain(node_with_rows, HUBS)
    assert set(sink) == {"https://a/ingest", "https://b/ingest"}
    mx = _maxid(node_with_rows)
    for url, _ in HUBS:
        dest = config.hub_dest(url)
        assert ship._last(node_with_rows, dest, "host_samples") == mx  # each hub's cursor at the full max


def test_compress_once_at_shared_frontier(node_with_rows, monkeypatch):
    sink = _stub_ok(monkeypatch)
    calls = {"gather": 0, "compress": 0}
    real_gather, real_compress = ship.gather, ship._compress
    monkeypatch.setattr(ship, "gather",
                        lambda c, d: (calls.__setitem__("gather", calls["gather"] + 1) or real_gather(c, d)))
    monkeypatch.setattr(ship, "_compress",
                        lambda p: (calls.__setitem__("compress", calls["compress"] + 1) or real_compress(p)))
    ship.drain(node_with_rows, HUBS)
    # two hubs at an identical fresh frontier -> ONE gather + ONE gzip, but a POST to each
    assert calls == {"gather": 1, "compress": 1}
    assert len(sink["https://a/ingest"]) == 1 and len(sink["https://b/ingest"]) == 1
    # same compressed bytes reused for both hubs
    assert sink["https://a/ingest"][0] == sink["https://b/ingest"][0]


def test_one_hub_down_is_isolated_then_recovers(node_with_rows, monkeypatch):
    down = {"https://b/ingest"}
    sink: dict[str, list] = {}

    def fake(url, secret, body):
        if url in down:
            return False
        sink.setdefault(url, []).append(body)
        return True

    monkeypatch.setattr(ship, "_post_body", fake)
    ship.drain(node_with_rows, HUBS)
    mx = _maxid(node_with_rows)
    a, b = config.hub_dest("https://a/ingest"), config.hub_dest("https://b/ingest")
    assert ship._last(node_with_rows, a, "host_samples") == mx   # A advanced
    assert ship._last(node_with_rows, b, "host_samples") == 0    # B untouched (isolated)

    down.clear()  # B comes back
    ship.drain(node_with_rows, HUBS)
    assert ship._last(node_with_rows, b, "host_samples") == mx    # B caught up on its backlog
    assert "https://b/ingest" in sink


def test_lagging_hub_triggers_own_gather(node_with_rows, monkeypatch):
    _stub_ok(monkeypatch)
    a = config.hub_dest("https://a/ingest")
    ship._set_last(node_with_rows, a, "host_samples", 2)  # A ahead, B fresh -> distinct frontiers
    node_with_rows.commit()
    calls = {"gather": 0}
    real_gather = ship.gather
    monkeypatch.setattr(ship, "gather",
                        lambda c, d: (calls.__setitem__("gather", calls["gather"] + 1) or real_gather(c, d)))
    ship.drain(node_with_rows, HUBS)
    assert calls["gather"] >= 2  # one gather per distinct frontier - laggard pays its own way
