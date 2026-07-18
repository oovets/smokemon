"""Hub-side alert delivery: the incident projection, severity gating, mute, and the
dedup / re-notify / resolve state machine.

Detection is not re-tested here -- the node's detector already applied debounce and hysteresis
before it wrote the transition. What is tested is that delivery keys off the incident uid, so
one occurrence pages once and a genuinely new occurrence is not swallowed by the previous one's
cooldown.
"""

from smokemon import alerts, config, notify

NOW = 1_000_000.0


def _live(seed, node):
    seed.heartbeat(node, NOW - 10)


# ---------- evaluate: the incident projection ----------

def test_evaluate_projects_open_incidents_keyed_by_uid(seed, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    _live(seed, "pi01")
    seed.incident("pi01", "uid-open", signal="ping.loss", entity="1.1.1.1",
                  severity="crit", opened_ts=NOW - 600)
    cur = alerts.evaluate(seed.conn, NOW)
    assert set(cur) == {"uid-open"}
    a = cur["uid-open"]
    assert a["key"] == "uid-open" and a["node"] == "pi01" and a["severity"] == 4
    assert a["kind"] == "ping.loss" and a["label"] == "1.1.1.1"


def test_evaluate_severity_gating(seed, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 3)   # page only errors and criticals
    _live(seed, "pi01")
    seed.incident("pi01", "uid-warn", severity="warn", opened_ts=NOW - 600)
    seed.incident("pi01", "uid-crit", severity="crit", opened_ts=NOW - 600)
    assert set(alerts.evaluate(seed.conn, NOW)) == {"uid-crit"}


def test_evaluate_skips_closed_incidents(seed, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    _live(seed, "pi01")
    seed.incident("pi01", "uid-done", severity="crit",
                  opened_ts=NOW - 900, closed_ts=NOW - 800)
    assert alerts.evaluate(seed.conn, NOW) == {}


def test_evaluate_skips_incidents_on_silent_nodes(seed, monkeypatch):
    """An alert for a node we can no longer hear from could never clear -- the close that would
    resolve it has nowhere to go."""
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    seed.heartbeat("silent01", NOW - 100_000)
    seed.incident("silent01", "uid-silent", severity="crit", opened_ts=NOW - 600)
    assert alerts.evaluate(seed.conn, NOW) == {}


def test_evaluate_ignores_mute(seed, monkeypatch):
    """Mute suppresses paging only; every alert is still tracked so the dashboard can show its
    firing-since even when nothing was sent."""
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", ["uid-*"])
    _live(seed, "pi01")
    seed.incident("pi01", "uid-muted", severity="crit", opened_ts=NOW - 600)
    assert set(alerts.evaluate(seed.conn, NOW)) == {"uid-muted"}


# ---------- paging gate ----------

def test_to_page_filters_mute_and_requires_url(monkeypatch):
    firing = [{"key": "uid-a"}, {"key": "uid-b"}]
    monkeypatch.setattr(config, "NOTIFY_URL", "")
    monkeypatch.setattr(config, "ALERT_MUTE", [])
    assert alerts.to_page(firing) == []                # no URL -> nothing is page-able

    monkeypatch.setattr(config, "NOTIFY_URL", "https://ntfy.sh/t")
    monkeypatch.setattr(config, "ALERT_MUTE", ["uid-b"])
    assert [a["key"] for a in alerts.to_page(firing)] == ["uid-a"]


def test_muted_alert_tracked_but_not_paged(seed, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", ["*"])
    monkeypatch.setattr(config, "NOTIFY_URL", "https://ntfy.sh/t")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(1) or True)
    _live(seed, "pi01")
    seed.incident("pi01", "uid-1", severity="crit", opened_ts=NOW - 600)

    cur = alerts.evaluate(seed.conn, NOW)
    firing, resolved = alerts.plan(cur, alerts.load_state(seed.conn), NOW)
    title, _ = alerts.render(alerts.to_page(firing), alerts.to_page(resolved))
    assert alerts.to_page(firing) == [] and title is None
    alerts.persist(seed.conn, cur, resolved, set(), NOW)
    assert sent == []
    assert set(alerts.load_state(seed.conn)) == set(cur)   # tracked all the same


# ---------- the flap-suppression state machine ----------

def _alert(uid="uid-1", severity=3):
    return {uid: {"key": uid, "node": "pi01", "kind": "ping.loss", "label": "1.1.1.1",
                  "severity": severity, "detail": "ping.loss/1.1.1.1 for 10m"}}


def test_plan_new_then_dedup_then_renotify(monkeypatch):
    monkeypatch.setattr(config, "ALERT_RENOTIFY_S", 1800)
    cur = _alert()
    firing, resolved = alerts.plan(cur, {}, 1000.0)
    assert [a["key"] for a in firing] == ["uid-1"] and resolved == []

    state = {"uid-1": {"severity": 3, "detail": "d", "first_ts": 1000.0, "notified_ts": 1000.0}}
    assert alerts.plan(cur, state, 1060.0)[0] == []                      # inside the cooldown
    assert [a["key"] for a in alerts.plan(cur, state, 2801.0)[0]] == ["uid-1"]

    state["uid-1"]["notified_ts"] = None       # an earlier send failed -> retry immediately
    assert [a["key"] for a in alerts.plan(cur, state, 1060.0)[0]] == ["uid-1"]


def test_a_new_occurrence_gets_a_new_uid_and_pages_despite_the_cooldown(monkeypatch):
    """Why uid is the right key. The node mints a new uid when a genuinely new occurrence
    begins, so it is not silenced by the previous occurrence's re-notify cooldown -- which a
    node/kind/label key would have done."""
    monkeypatch.setattr(config, "ALERT_RENOTIFY_S", 1800)
    state = {"uid-old": {"severity": 3, "detail": "d", "first_ts": 1000.0, "notified_ts": 1000.0}}
    firing, resolved = alerts.plan(_alert("uid-new"), state, 1060.0)
    assert [a["key"] for a in firing] == ["uid-new"]
    assert [a["key"] for a in resolved] == ["uid-old"]   # and the old one resolves out


def test_plan_resolved():
    state = {"uid-1": {"severity": 3, "detail": "d", "first_ts": 1.0, "notified_ts": 1.0}}
    firing, resolved = alerts.plan({}, state, 2.0)
    assert firing == [] and [a["key"] for a in resolved] == ["uid-1"]


# ---------- rendering ----------

def test_render_empty():
    assert alerts.render([], []) == (None, None)


def test_render_firing_and_resolved(monkeypatch):
    monkeypatch.setattr(config, "ALERT_NOTIFY_RESOLVED", True)
    firing = list(_alert().values())
    resolved = [{"key": "uid-2", "node": "pi02", "kind": "host.mem", "label": "-"}]
    title, body = alerts.render(firing, resolved)
    assert "pi01" in title and "FIRING" in body
    # the resolved line names the node from tracked state, not by splitting the opaque uid
    assert "[RESOLVED] pi02 host.mem/-" in body
    assert "uid-2" not in body


def test_render_titles_the_worst_and_counts_the_rest():
    firing = [*_alert("uid-a", severity=2).values(), *_alert("uid-b", severity=4).values()]
    title, _ = alerts.render(firing, [])
    assert "(+1 more)" in title


# ---------- persistence ----------

def test_persist_roundtrip(hub_conn):
    cur = _alert()
    alerts.persist(hub_conn, cur, [], {"uid-1"}, 1000.0)
    state = alerts.load_state(hub_conn)
    assert state["uid-1"]["notified_ts"] == 1000.0
    # node/kind/label survive the roundtrip so a resolved alert can still be named
    assert state["uid-1"]["node"] == "pi01" and state["uid-1"]["kind"] == "ping.loss"

    alerts.persist(hub_conn, {}, [{"key": "uid-1"}], set(), 1100.0)
    assert alerts.load_state(hub_conn) == {}


def test_persist_keeps_first_ts_across_passes(hub_conn):
    """firing-since must not reset every pass, or the dashboard shows every alert as brand new."""
    cur = _alert()
    alerts.persist(hub_conn, cur, [], {"uid-1"}, 1000.0)
    alerts.persist(hub_conn, cur, [], set(), 1600.0)
    assert alerts.load_state(hub_conn)["uid-1"]["first_ts"] == 1000.0


def test_full_pass_sends_once_then_goes_quiet(seed, monkeypatch):
    """evaluate -> plan -> render -> send -> persist, then a second pass inside the cooldown
    is silent."""
    monkeypatch.setattr(config, "NOTIFY_MIN_SEVERITY", 2)
    monkeypatch.setattr(config, "ALERT_MUTE", [])
    monkeypatch.setattr(config, "NOTIFY_URL", "https://ntfy.sh/t")
    monkeypatch.setattr(config, "ALERT_RENOTIFY_S", 1800)
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, body, *a, **k: sent.append(title) or True)
    _live(seed, "pi01")
    seed.incident("pi01", "uid-1", severity="crit", opened_ts=NOW - 600)

    def one_pass(now):
        cur = alerts.evaluate(seed.conn, now)
        firing, resolved = alerts.plan(cur, alerts.load_state(seed.conn), now)
        page = alerts.to_page(firing)
        title, body = alerts.render(page, alerts.to_page(resolved))
        ok = notify.send(title, body) if title else False
        alerts.persist(seed.conn, cur, resolved, {a["key"] for a in page} if ok else set(), now)

    one_pass(NOW)
    one_pass(NOW + 60)
    assert len(sent) == 1
