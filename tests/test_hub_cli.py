"""`smoke hub`: hub-URL normalization + env-file read/write that repoint a node's
shipper target. Pure helpers, no network."""

from smokemon import cli


def test_normalize_hub_url():
    n = cli._normalize_hub_url
    assert n("new-hub.ip") == "http://new-hub.ip:8765/ingest"
    assert n("10.0.0.5:9000") == "http://10.0.0.5:9000/ingest"
    assert n("http://h:8765/ingest") == "http://h:8765/ingest"
    assert n("https://hub.example.com") == "https://hub.example.com:8765/ingest"
    assert n("host/custom") == "http://host:8765/custom"


def test_env_get_missing(tmp_path):
    assert cli._env_get(str(tmp_path / "nope.env"), "SMOKEMON_HUB_URL") is None


def test_env_set_creates_then_replaces(tmp_path):
    f = tmp_path / "smokemon.env"
    ok, _ = cli._env_set(str(f), "SMOKEMON_HUB_URL", "http://a:8765/ingest")
    assert ok and cli._env_get(str(f), "SMOKEMON_HUB_URL") == "http://a:8765/ingest"

    # replacing must not duplicate, and must leave other keys untouched
    f.write_text("SMOKEMON_NODE=pi01\nSMOKEMON_HUB_URL=old\nSMOKEMON_HUB_SECRET=s\n")
    cli._env_set(str(f), "SMOKEMON_HUB_URL", "http://b:8765/ingest")
    body = f.read_text()
    assert body.count("SMOKEMON_HUB_URL=") == 1
    assert "http://b:8765/ingest" in body
    assert "SMOKEMON_NODE=pi01" in body and "SMOKEMON_HUB_SECRET=s" in body
