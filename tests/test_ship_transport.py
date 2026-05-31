"""Shipper transport guard: the shared secret must not cross the wire in clear."""

from smokemon import config, ship


def test_https_allowed():
    assert ship.hub_url_ok("https://hub.example/ingest")[0]


def test_http_loopback_allowed():
    assert ship.hub_url_ok("http://127.0.0.1:8765/ingest")[0]
    assert ship.hub_url_ok("http://localhost:8765/ingest")[0]


def test_http_remote_rejected():
    ok, reason = ship.hub_url_ok("http://hub.example/ingest")
    assert not ok
    assert "https" in reason.lower()


def test_insecure_override(monkeypatch):
    monkeypatch.setattr(config, "HUB_INSECURE", True)
    assert ship.hub_url_ok("http://hub.example/ingest")[0]
