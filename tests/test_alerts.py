"""Hub-side alert delivery: detection reuse + severity gating + mute, and the
dedup / re-notify / resolve flap-suppression state machine."""

import pytest

from smokemon import alerts, config, core, schema


@pytest.fixture
def hub_conn(hub_db):
    """Initialized hub-schema SQLite connection seeded by individual tests."""
    conn = core.connect(str(hub_db))
    schema.init_hub(conn)
    try:
        yield conn
    finally:
        conn.close()


def _seed_services(conn, ts0):
    """One docker row (running+unhealthy, sev2) + one proc row (gst down, sev3) +
    one stream row (RTSP failing, sev2) - the same shape as test_logs._seed_services."""
    schema.insert(conn, "docker_samples", [{"ts": ts0, "name": "api", "state": "running",
                  "running": 1, "health": "unhealthy"}], node="pi01")
    schema.insert(conn, "proc_watch", [{"ts": ts0, "label": "gst", "count": 0}], node="pi01")
    schema.insert(conn, "stream_probes", [{"ts": ts0, "url": "rtsp://cam/imx519", "ok": 0,
                  "status": "timeout"}], node="pi01")
    conn.commit()


def test_evaluate_detects_services(hub_conn, ts0, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", [])
    _seed_services(hub_conn, ts0)
    cur = alerts.evaluate(hub_conn, ts0 + 100)
    kinds = {a["kind"] for a in cur.values()}
    assert {"docker", "proc", "stream"} <= kinds
    assert "pi01/proc/gst" in cur and cur["pi01/proc/gst"]["severity"] == 3


def test_evaluate_severity_gating(hub_conn, ts0, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 3)  # page only criticals
    monkeypatch.setattr(config, "ALERT_MUTE", [])
    _seed_services(hub_conn, ts0)
    cur = alerts.evaluate(hub_conn, ts0 + 100)
    assert set(cur) == {"pi01/proc/gst"}  # only the sev-3 process-missing survives


def test_evaluate_ignores_mute(hub_conn, ts0, monkeypatch):
    """Mute no longer filters evaluate - every firing alert is tracked regardless."""
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", ["pi01/proc/*"])
    _seed_services(hub_conn, ts0)
    cur = alerts.evaluate(hub_conn, ts0 + 100)
    assert "pi01/proc/gst" in cur  # still evaluated even though muted
    assert {a["kind"] for a in cur.values()} == {"docker", "proc", "stream"}


def test_to_page_filters_mute_and_requires_url(monkeypatch):
    firing = [{"key": "pi01/proc/gst"}, {"key": "pi01/stream/cam"}]
    # no URL -> nothing is page-able, even unmuted
    monkeypatch.setattr(config, "NOTIFY_URL", "")
    monkeypatch.setattr(config, "ALERT_MUTE", [])
    assert alerts.to_page(firing) == []
    # URL set, one muted -> only the unmuted key pages
    monkeypatch.setattr(config, "NOTIFY_URL", "https://ntfy.sh/t")
    monkeypatch.setattr(config, "ALERT_MUTE", ["pi01/stream/*"])
    assert [a["key"] for a in alerts.to_page(firing)] == ["pi01/proc/gst"]


def test_muted_alert_tracked_but_not_paged(hub_conn, ts0, monkeypatch):
    """A muted firing alert lands in alert_state (so the dashboard shows firing-since) but the
    full pass never calls notify.send for it."""
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", ["*"])  # mute everything
    monkeypatch.setattr(config, "NOTIFY_URL", "https://ntfy.sh/t")
    sent = []
    monkeypatch.setattr("smokemon.notify.send", lambda *a, **k: sent.append(1) or True)
    _seed_services(hub_conn, ts0)
    now = ts0 + 100
    cur = alerts.evaluate(hub_conn, now)
    firing, resolved = alerts.plan(cur, alerts.load_state(hub_conn), now)
    page = alerts.to_page(firing)
    title, body = alerts.render(page, alerts.to_page(resolved))
    assert page == [] and title is None                  # nothing page-able
    alerts.persist(hub_conn, cur, resolved, set(), now)
    assert sent == []                                    # never paged
    assert set(alerts.load_state(hub_conn)) == set(cur)  # but all tracked


def test_plan_new_then_dedup_then_renotify(monkeypatch):
    monkeypatch.setattr(config, "ALERT_RENOTIFY_S", 1800)
    cur = {"pi01/proc/gst": {"key": "pi01/proc/gst", "node": "pi01", "kind": "proc",
                             "label": "gst", "severity": 3, "detail": "process missing"}}
    # brand new -> fires
    firing, resolved = alerts.plan(cur, {}, 1000.0)
    assert [a["key"] for a in firing] == ["pi01/proc/gst"] and resolved == []
    # already notified, cooldown not elapsed -> silent
    state = {"pi01/proc/gst": {"severity": 3, "detail": "process missing",
                               "first_ts": 1000.0, "notified_ts": 1000.0}}
    firing, _ = alerts.plan(cur, state, 1000.0 + 60)
    assert firing == []
    # cooldown elapsed -> re-fires
    firing, _ = alerts.plan(cur, state, 1000.0 + 1801)
    assert [a["key"] for a in firing] == ["pi01/proc/gst"]
    # earlier send failed (notified_ts None) -> retried immediately
    state["pi01/proc/gst"]["notified_ts"] = None
    firing, _ = alerts.plan(cur, state, 1000.0 + 60)
    assert [a["key"] for a in firing] == ["pi01/proc/gst"]


def test_plan_resolved():
    state = {"pi01/proc/gst": {"severity": 3, "detail": "process missing",
                               "first_ts": 1.0, "notified_ts": 1.0}}
    firing, resolved = alerts.plan({}, state, 2.0)
    assert firing == [] and [a["key"] for a in resolved] == ["pi01/proc/gst"]


def test_render_empty():
    assert alerts.render([], []) == (None, None)


def test_render_firing_and_resolved(monkeypatch):
    monkeypatch.setattr(config, "ALERT_NOTIFY_RESOLVED", True)
    firing = [{"key": "pi01/proc/gst", "node": "pi01", "kind": "proc", "label": "gst",
               "severity": 3, "detail": "process missing"}]
    resolved = [{"key": "pi02/stream/cam"}]
    title, body = alerts.render(firing, resolved)
    assert "pi01" in title and "FIRING" in body and "RESOLVED" in body


def test_persist_roundtrip(hub_conn):
    cur = {"pi01/proc/gst": {"key": "pi01/proc/gst", "node": "pi01", "kind": "proc",
                             "label": "gst", "severity": 3, "detail": "process missing"}}
    alerts.persist(hub_conn, cur, [], {"pi01/proc/gst"}, 1000.0)
    state = alerts.load_state(hub_conn)
    assert state["pi01/proc/gst"]["notified_ts"] == 1000.0
    # resolving deletes the row
    alerts.persist(hub_conn, {}, [{"key": "pi01/proc/gst"}], set(), 1100.0)
    assert alerts.load_state(hub_conn) == {}


def test_risks_annotates_alerts(hub_conn, ts0, monkeypatch):
    """The Risk tab's /api/risks alerts carry muted/since_s/notified after a delivery pass."""
    from smokemon import hubapi
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", ["pi01/stream/*"])
    _seed_services(hub_conn, ts0)
    now = ts0 + 100
    # record delivery state so first_ts/notified_ts are populated for the proc alert
    cur = alerts.evaluate(hub_conn, now)
    alerts.persist(hub_conn, cur, [], {"pi01/proc/gst"}, now)
    out = {f"{a['node']}/{a['kind']}/{a.get('label', '')}": a
           for a in hubapi.risks(hub_conn, 24, now=now)["alerts"]}
    gst = out["pi01/proc/gst"]
    assert gst["notified"] is True and gst["since_s"] == 0 and gst["muted"] is False
    # stream alert is muted by the glob: still tracked (since_s set) but never paged (notified False)
    stream = next(a for a in out.values() if a["kind"] == "stream")
    assert stream["muted"] is True and stream["since_s"] == 0 and stream["notified"] is False


def test_full_pass_sends_once(hub_conn, ts0, monkeypatch):
    """evaluate -> plan -> render -> (send) -> persist, then a second pass is silent."""
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", [])
    monkeypatch.setattr(config, "ALERT_RENOTIFY_S", 1800)
    sent = []
    monkeypatch.setattr("smokemon.notify.send",
                        lambda title, body, *a, **k: sent.append(title) or True)
    _seed_services(hub_conn, ts0)

    def one_pass(now):
        from smokemon import notify
        cur = alerts.evaluate(hub_conn, now)
        firing, resolved = alerts.plan(cur, alerts.load_state(hub_conn), now)
        title, body = alerts.render(firing, resolved)
        ok = notify.send(title, body) if title else False
        alerts.persist(hub_conn, cur, resolved, {a["key"] for a in firing} if ok else set(), now)

    one_pass(ts0 + 100)
    one_pass(ts0 + 160)  # within cooldown -> nothing new
    assert len(sent) == 1  # paged once for the batch, then silent
