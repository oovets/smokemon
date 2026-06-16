"""S4 push/webhook alerting: per-kind payload shaping, URL-kind detection, severity
gating and end-to-end alert_from_db with the network send stubbed out."""

import json

from smokemon import core, notify, schema


def test_detect_kind():
    assert notify.detect_kind("https://ntfy.sh/mytopic") == "ntfy"
    assert notify.detect_kind("https://hooks.slack.com/services/T/B/X") == "slack"
    assert notify.detect_kind("https://discord.com/api/webhooks/1/abc") == "discord"
    assert notify.detect_kind("https://api.incident.io/v2/alert_events/http/01ABC") == "incident_io"
    assert notify.detect_kind("https://example.com/hook") == "generic"


def test_is_per_alert(monkeypatch):
    # only incident.io wants per-alert events; everything else takes the batched digest
    assert notify.is_per_alert("https://api.incident.io/v2/alert_events/http/x") is True
    assert notify.is_per_alert("https://ntfy.sh/t") is False
    # an explicit kind pin wins over the URL host
    assert notify.is_per_alert("https://example.com/hook", kind="incident_io") is True
    # falls back to the configured URL when none is passed
    monkeypatch.setattr("smokemon.config.NOTIFY_URL", "https://api.incident.io/v2/alert_events/http/x")
    monkeypatch.setattr("smokemon.config.NOTIFY_KIND", "")
    assert notify.is_per_alert() is True


def test_build_event_request():
    r = notify.build_event_request(
        "https://api.incident.io/v2/alert_events/http/x", "pi01/proc/gst", "firing",
        "smokemon: pi01 proc/gst", "process missing", {"node": "pi01", "severity": 3}, token="tok")
    body = json.loads(r.data)
    assert body["deduplication_key"] == "pi01/proc/gst"
    assert body["status"] == "firing"
    assert body["title"] == "smokemon: pi01 proc/gst"
    assert body["description"] == "process missing"
    assert body["metadata"] == {"node": "pi01", "severity": 3}
    assert r.headers["Authorization"] == "Bearer tok"
    assert r.get_method() == "POST"
    # resolved event with the same dedup_key, empty description falls back to the title
    r2 = notify.build_event_request("https://api.incident.io/x", "pi01/proc/gst", "resolved",
                                    "smokemon: pi01 proc/gst", "", token="")
    body2 = json.loads(r2.data)
    assert body2["status"] == "resolved" and body2["description"] == "smokemon: pi01 proc/gst"
    assert "Authorization" not in r2.headers   # no token -> no auth header


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
        {"start": 1_000_000.0, "klass": "packet-loss", "scope": "tailscale",
         "detail": "loss peaked 15%", "duration_s": 10, "severity": 1},
        {"start": 1_000_100.0, "klass": "isp-outage", "scope": "internet",
         "detail": "loss peaked 100%", "duration_s": 30, "severity": 3},
    ]
    title, body = notify.summarize_incidents(incidents, node="app01", min_severity=2)
    assert "app01" in title and "isp-outage" in title
    assert "isp-outage" in body
    assert "packet-loss" not in body          # severity 1 filtered out
    # nothing qualifies -> (None, None), caller sends nothing.
    assert notify.summarize_incidents(incidents, min_severity=9) == (None, None)


def test_alert_from_db_sends_when_outage(tmp_db, ts0, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    # internet 100% loss, gw clean -> isp-outage (severity 3).
    rows = []
    for i in range(4):
        rows.append({"ts": ts0 + i * 10, "target": "1.1.1.1", "sent": 20, "recv": 0,
                     "loss_pct": 100.0, "rtt_min": None, "rtt_p25": None, "rtt_median": None,
                     "rtt_p75": None, "rtt_avg": None, "rtt_max": None, "rtt_stddev": None})
        rows.append({"ts": ts0 + i * 10, "target": "192.168.0.1", "sent": 20, "recv": 20,
                     "loss_pct": 0.0, "rtt_min": 1.0, "rtt_p25": 1.0, "rtt_median": 1.0,
                     "rtt_p75": 1.0, "rtt_avg": 1.0, "rtt_max": 2.0, "rtt_stddev": 0.1})
    schema.insert(conn, "ping_runs", rows)
    conn.commit()

    captured = {}

    def fake_send(title, body, url=None, kind=None, timeout=15):
        captured["title"] = title
        captured["body"] = body
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    n = notify.alert_from_db(conn, ts0 - 30, ts0 + 60, min_severity=2)
    assert n >= 1
    assert "isp-outage" in captured["title"]
    conn.close()


def test_alert_from_db_silent_when_clear(tmp_db, ts0, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "ping_runs", [{
        "ts": ts0, "target": "1.1.1.1", "sent": 20, "recv": 20, "loss_pct": 0.0,
        "rtt_min": 8.0, "rtt_p25": 8.0, "rtt_median": 8.0, "rtt_p75": 8.0,
        "rtt_avg": 8.0, "rtt_max": 9.0, "rtt_stddev": 0.2}])
    conn.commit()
    monkeypatch.setattr(notify, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not send")))
    assert notify.alert_from_db(conn, ts0 - 60, ts0 + 60, min_severity=2) == 0
    conn.close()
