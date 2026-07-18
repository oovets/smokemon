"""S4 push/webhook alerting: per-kind payload shaping, URL-kind detection, severity
gating and end-to-end alert_from_db with the network send stubbed out."""

import json

from smokemon import core, notify, schema


def test_detect_kind():
    assert notify.detect_kind("https://ntfy.sh/mytopic") == "ntfy"
    assert notify.detect_kind("https://hooks.slack.com/services/T/B/X") == "slack"
    assert notify.detect_kind("https://discord.com/api/webhooks/1/abc") == "discord"
    assert notify.detect_kind("https://example.com/hook") == "generic"


def test_build_request_per_kind():
    r = notify.build_request("https://ntfy.sh/t", "Title", "line1\nline2", "ntfy")
    assert r.data == b"line1\nline2"
    assert r.headers["Title"] == "Title"

    r = notify.build_request("https://hooks.slack.com/x", "T", "B", "slack")
    assert json.loads(r.data)["text"] == "*T*\nB"

    r = notify.build_request("https://discord.com/x", "T", "B", "discord")
    assert json.loads(r.data)["content"] == "**T**\nB"

    r = notify.build_request("https://x/y", "T", "B", "generic")
    body = json.loads(r.data)
    assert body["title"] == "T" and body["body"] == "B" and body["source"] == "smokemon"


def test_summarize_gates_on_severity():
    incidents = [
        {"uid": "a", "signal": "loss", "entity": "tailscale", "severity": "warn",
         "opened_ts": 1_000_000.0, "duration_s": 10.0, "state": "closed"},
        {"uid": "b", "signal": "loss", "entity": "1.1.1.1", "severity": "crit",
         "opened_ts": 1_000_100.0, "duration_s": 30.0, "state": "closed"},
    ]
    title, body = notify.summarize_incidents(incidents, node="app01", min_severity=2)
    assert "app01" in title and "1.1.1.1" in title
    assert "crit" in body
    assert "tailscale" not in body            # warn ranks below the bar, filtered out
    # nothing qualifies -> (None, None), caller sends nothing.
    assert notify.summarize_incidents(incidents, min_severity=9) == (None, None)


def test_alert_from_db_sends_when_incident_open(tmp_db, ts0, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "incidents", [
        {"ts": ts0, "uid": "u1", "transition": "open", "signal": "loss", "entity": "1.1.1.1",
         "severity": "crit", "value": 100.0, "opened_ts": ts0},
        {"ts": ts0 + 40, "uid": "u1", "transition": "close", "signal": "loss", "entity": "1.1.1.1",
         "severity": "info", "opened_ts": ts0, "duration_s": 40.0, "worst_value": 100.0},
    ])
    conn.commit()

    captured = {}

    def fake_send(title, body, url=None, kind=None, timeout=15):
        captured["title"] = title
        captured["body"] = body
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    n = notify.alert_from_db(conn, ts0 - 30, ts0 + 60, min_severity=2)
    assert n >= 1
    assert "loss 1.1.1.1" in captured["title"]
    conn.close()


def test_alert_from_db_silent_when_clear(tmp_db, ts0, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "heartbeats", [{"ts": ts0, "interval_s": 300.0}])
    conn.commit()
    monkeypatch.setattr(notify, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not send")))
    assert notify.alert_from_db(conn, ts0 - 60, ts0 + 60, min_severity=2) == 0
    conn.close()
