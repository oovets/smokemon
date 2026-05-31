"""Per-application network throughput (hubapi.network): cumulative-byte gauge -> bytes/s via
positive bucket deltas, aggregated by app(port) fleet-wide or filtered to one node."""

import time

import pytest

from smokemon import core, hubapi, schema


@pytest.fixture
def hubc(hub_db):
    c = core.connect(str(hub_db))
    schema.init_hub(c)
    yield c
    c.close()


def _samp(c, node, port, d, bsent, brecv, ago, now):
    schema.insert(c, "port_samples", [{"ts": now - ago, "proto": "tcp", "dir": d, "port": port,
                  "conns": 1, "peers": 1, "listening": 1 if d == "in" else 0,
                  "bytes_sent": bsent, "bytes_recv": brecv}], node=node)


def test_app_label():
    assert hubapi.app_label(443) == "https" and hubapi.app_label(6379) == "redis"
    assert hubapi.app_label(40000) == ":40000"  # unknown -> bare port


def test_network_throughput_delta(hubc):
    now = time.time()  # gauge 0 -> 360000 across a ~360s bucket width => 1000 B/s
    _samp(hubc, "pi01", 443, "in", 0, 0, 400, now)
    _samp(hubc, "pi01", 443, "in", 180000, 180000, 40, now)
    hubc.commit()
    d = hubapi.network(hubc, None, 6.0, now=now)
    https = next(a for a in d["apps"] if a["port"] == 443)
    assert https["app"] == "https" and https["total"] > 0
    assert max(https["series"]) == pytest.approx(1000, abs=1)


def test_network_fleet_sums_across_nodes(hubc):
    now = time.time()
    for nd in ("pi01", "pi02"):
        _samp(hubc, nd, 443, "in", 0, 0, 400, now)
        _samp(hubc, nd, 443, "in", 180000, 180000, 40, now)
    hubc.commit()
    https = next(a for a in hubapi.network(hubc, None, 6.0, now=now)["apps"] if a["port"] == 443)
    assert max(https["series"]) == pytest.approx(2000, abs=2)  # both nodes summed into the app


def test_network_node_filter(hubc):
    now = time.time()
    _samp(hubc, "pi01", 443, "in", 0, 0, 400, now)
    _samp(hubc, "pi01", 443, "in", 180000, 180000, 40, now)
    _samp(hubc, "pi02", 6379, "out", 0, 0, 400, now)
    _samp(hubc, "pi02", 6379, "out", 0, 36000, 40, now)
    hubc.commit()
    d = hubapi.network(hubc, "pi01", 6.0, now=now)
    assert d["node"] == "pi01" and {a["port"] for a in d["apps"]} == {443}


def test_network_negative_delta_clamped(hubc):
    now = time.time()  # gauge drops (connection closed) -> never a negative rate
    _samp(hubc, "pi01", 22, "in", 500000, 500000, 400, now)
    _samp(hubc, "pi01", 22, "in", 0, 0, 40, now)
    hubc.commit()
    ssh = next((a for a in hubapi.network(hubc, None, 6.0, now=now)["apps"] if a["port"] == 22), None)
    assert ssh is not None and all(v >= 0 for v in ssh["series"]) and ssh["total"] == 0.0


def test_network_topn_cap(hubc):
    now = time.time()
    for p in range(20):  # 20 distinct ports; node view caps at 12
        _samp(hubc, "pi01", 1000 + p, "in", 0, 0, 400, now)
        _samp(hubc, "pi01", 1000 + p, "in", (p + 1) * 1000, 0, 40, now)
    hubc.commit()
    assert len(hubapi.network(hubc, "pi01", 6.0, now=now)["apps"]) == 12
