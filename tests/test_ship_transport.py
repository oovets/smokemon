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


# --- multi-hub config resolution (config._hubs) ---

def test_hubs_backcompat_single(monkeypatch):
    monkeypatch.setattr(config, "HUB_URL", "https://only/ingest")
    monkeypatch.setattr(config, "HUB_SECRET", "sek")
    monkeypatch.delenv("SMOKEMON_HUB_URLS", raising=False)
    monkeypatch.delenv("SMOKEMON_HUB_SECRETS", raising=False)
    assert config._hubs() == [("https://only/ingest", "sek")]


def test_hubs_multi_shared_secret(monkeypatch):
    monkeypatch.setattr(config, "HUB_SECRET", "shared")
    monkeypatch.setenv("SMOKEMON_HUB_URLS", "https://a/ingest;https://b/ingest")
    monkeypatch.delenv("SMOKEMON_HUB_SECRETS", raising=False)
    assert config._hubs() == [("https://a/ingest", "shared"), ("https://b/ingest", "shared")]


def test_hubs_positional_secret_override(monkeypatch):
    # empty first slot -> shared secret; second slot overrides
    monkeypatch.setattr(config, "HUB_SECRET", "shared")
    monkeypatch.setenv("SMOKEMON_HUB_URLS", "https://a/ingest;https://b/ingest")
    monkeypatch.setenv("SMOKEMON_HUB_SECRETS", ";secretB")
    assert config._hubs() == [("https://a/ingest", "shared"), ("https://b/ingest", "secretB")]


def test_hubs_dedup_preserves_order(monkeypatch):
    monkeypatch.setattr(config, "HUB_SECRET", "s")
    monkeypatch.setenv("SMOKEMON_HUB_URLS", "https://a/ingest;https://b/ingest;https://a/ingest")
    monkeypatch.delenv("SMOKEMON_HUB_SECRETS", raising=False)
    assert config._hubs() == [("https://a/ingest", "s"), ("https://b/ingest", "s")]


# --- valid_hubs filtering + main() exit contract ---

def test_valid_hubs_drops_insecure():
    hubs = [("https://a/ingest", "s"), ("http://hub.example/ingest", "s")]
    assert ship.valid_hubs(hubs) == [("https://a/ingest", "s")]


def test_valid_hubs_keeps_safe_transports():
    hubs = [("https://a/ingest", "s"), ("http://127.0.0.1:8765/ingest", "s")]
    assert ship.valid_hubs(hubs) == hubs


def test_main_all_invalid_returns_2(monkeypatch):
    # one (or more) configured hub, none valid -> loud exit 2, no DB touched
    monkeypatch.setattr(config, "HUBS", [("http://hub.example/ingest", "s")])
    assert ship.main() == 2


def test_main_no_hub_returns_0(monkeypatch):
    monkeypatch.setattr(config, "HUBS", [])
    assert ship.main() == 0
