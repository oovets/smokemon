"""`smoke hub`: hub-URL normalization + env-file read/write that repoint a node's
shipper target. Pure helpers, no network."""

import argparse
import sys

from smokemon import cli, config


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


def test_env_unset_removes_key(tmp_path):
    f = tmp_path / "smokemon.env"
    f.write_text("SMOKEMON_NODE=pi01\nSMOKEMON_HUB_URLS=a;b\nSMOKEMON_HUB_SECRET=s\n")
    cli._env_unset(str(f), "SMOKEMON_HUB_URLS")
    body = f.read_text()
    assert "SMOKEMON_HUB_URLS" not in body
    assert "SMOKEMON_NODE=pi01" in body and "SMOKEMON_HUB_SECRET=s" in body


def _run_hub(tmp_path, monkeypatch, hosts, initial=""):
    """Drive `smoke hub HOSTS...` against an isolated env file on the Linux write-path,
    with hub reachability stubbed out so the helper does no network."""
    f = tmp_path / "smokemon.env"
    if initial:
        f.write_text(initial)
    monkeypatch.setattr(config, "ENV_FILE", str(f))
    monkeypatch.setattr(cli, "_hub_status", lambda u: "reachable")
    monkeypatch.setattr(sys, "platform", "linux")
    cli._hub(argparse.Namespace(hosts=hosts))
    return f.read_text()


def test_hub_set_single_writes_url_and_clears_urls(tmp_path, monkeypatch):
    body = _run_hub(tmp_path, monkeypatch, ["new-hub"],
                    initial="SMOKEMON_HUB_URLS=http://old-a:8765/ingest;http://old-b:8765/ingest\n")
    lines = body.splitlines()
    assert "SMOKEMON_HUB_URL=http://new-hub:8765/ingest" in lines
    assert not any(ln.startswith("SMOKEMON_HUB_URLS=") for ln in lines)  # stale list cleared


def test_hub_set_multiple_writes_urls_and_clears_single(tmp_path, monkeypatch):
    body = _run_hub(tmp_path, monkeypatch, ["a", "b"],
                    initial="SMOKEMON_HUB_URL=http://old:8765/ingest\n")
    lines = body.splitlines()
    assert "SMOKEMON_HUB_URLS=http://a:8765/ingest;http://b:8765/ingest" in lines
    assert not any(ln.startswith("SMOKEMON_HUB_URL=") for ln in lines)  # single var superseded


def test_hub_set_multiple_dedups(tmp_path, monkeypatch):
    body = _run_hub(tmp_path, monkeypatch, ["a", "a", "b"])
    urls = next(ln for ln in body.splitlines() if ln.startswith("SMOKEMON_HUB_URLS="))
    assert urls == "SMOKEMON_HUB_URLS=http://a:8765/ingest;http://b:8765/ingest"
