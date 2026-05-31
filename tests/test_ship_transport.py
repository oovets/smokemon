"""Shipper transport guard: the shared secret must not cross the wire in clear."""

import time

from smokemon import config, core, schema, ship


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


# --- SHIP_EXCLUDE: stop shipping tables the hub does not consume (kept node-local) ---

def test_ship_exclude_default_drops_synthetic():
    """Out of the box (no env override) synthetic_samples is excluded: it has no hub reader, so
    shipping it is dead weight. The default set is baked into config."""
    assert "synthetic_samples" in config._SHIP_EXCLUDE_DEFAULT
    assert "synthetic_samples" in config.SHIP_EXCLUDE


def test_ordered_tables_drops_default_exclusions(monkeypatch):
    # with only the baked-in default, every table except the default exclusions ships
    monkeypatch.setattr(config, "SHIP_EXCLUDE", config._SHIP_EXCLUDE_DEFAULT)
    ordered = ship._ordered_tables()
    assert "synthetic_samples" not in ordered
    assert set(ordered) == set(schema.STD_TABLES) - config._SHIP_EXCLUDE_DEFAULT
    assert ordered[0] == "ext_events" and ordered[1] == "log_excerpts"  # priority preserved


def test_ship_exclude_env_adds_to_default(monkeypatch):
    """SMOKEMON_SHIP_EXCLUDE ADDS to the default rather than replacing it, so a user adding their
    own exclusion never silently re-enables a known dead-weight table; SHIP_INCLUDE force-ships."""
    import importlib
    monkeypatch.setenv("SMOKEMON_SHIP_EXCLUDE", "gpu_samples")
    monkeypatch.delenv("SMOKEMON_SHIP_INCLUDE", raising=False)
    importlib.reload(config)
    try:
        expected = frozenset({"synthetic_samples", "gpu_samples"})
        assert expected == config.SHIP_EXCLUDE
        # SHIP_INCLUDE wins: force-ship a defaulted table
        monkeypatch.setenv("SMOKEMON_SHIP_INCLUDE", "synthetic_samples")
        importlib.reload(config)
        assert "synthetic_samples" not in config.SHIP_EXCLUDE
        assert "gpu_samples" in config.SHIP_EXCLUDE
    finally:
        monkeypatch.delenv("SMOKEMON_SHIP_EXCLUDE", raising=False)
        monkeypatch.delenv("SMOKEMON_SHIP_INCLUDE", raising=False)
        importlib.reload(config)


def test_ordered_tables_excludes_configured(monkeypatch):
    monkeypatch.setattr(config, "SHIP_EXCLUDE", frozenset({"synthetic_samples"}))
    ordered = ship._ordered_tables()
    assert "synthetic_samples" not in ordered
    # everything else is still there, and the priority tables still lead
    assert set(ordered) == set(schema.STD_TABLES) - {"synthetic_samples"}
    assert ordered[0] == "ext_events" and ordered[1] == "log_excerpts"


def test_ordered_tables_can_exclude_a_priority_table(monkeypatch):
    monkeypatch.setattr(config, "SHIP_EXCLUDE", frozenset({"log_excerpts"}))
    ordered = ship._ordered_tables()
    assert "log_excerpts" not in ordered
    assert ordered[0] == "ext_events"  # the remaining priority table still leads


def test_gather_skips_excluded_table(tmp_db, monkeypatch):
    """An excluded table with real rows must not appear in the gathered payload, while a
    non-excluded table written in the same cycle still ships."""
    monkeypatch.setattr(config, "SHIP_EXCLUDE", frozenset({"synthetic_samples"}))
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    ship.init_state(conn)
    ts = time.time()
    schema.insert(conn, "synthetic_samples",
                  [{"ts": ts, "probe": "doh", "ok": 1, "latency_ms": 12.0, "detail": ""}])
    schema.insert(conn, "host_samples", [{"ts": ts, "cpu_pct": 20.0}])
    conn.commit()
    payload, maxids = ship.gather(conn, "d")
    assert "synthetic_samples" not in payload and "synthetic_samples" not in maxids
    assert "host_samples" in payload  # a non-excluded table written the same cycle still ships
    conn.close()
