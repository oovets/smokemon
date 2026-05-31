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


def test_http_tailscale_allowed():
    # the tailnet is WireGuard-encrypted, so http to a 100.64/10 (or IPv6 ULA) hub is fine
    assert ship.hub_url_ok("http://100.127.203.7:8765/ingest") == (True, "tailscale")
    assert ship.hub_url_ok("http://100.64.0.1:8765/ingest")[0]
    assert ship.hub_url_ok("http://[fd7a:115c:a1e0::1]:8765/ingest")[0]


def test_http_non_tailscale_cgnat_edge_still_rejected():
    # 100.128.x is just outside Tailscale's 100.64.0.0/10 -> not auto-trusted
    ok, _ = ship.hub_url_ok("http://100.128.0.1:8765/ingest")
    assert not ok


def test_insecure_override(monkeypatch):
    monkeypatch.setattr(config, "HUB_INSECURE", True)
    assert ship.hub_url_ok("http://hub.example/ingest")[0]
