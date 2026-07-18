"""`smoke incidents` / `smoke incident` and the fleet renderers they share.

The load-bearing assertion in this file is the ongoing/unknown distinction. The hub reports
`unknown` when a node went silent while an incident was open, and rendering that as `ongoing`
would present a guess as a fact -- so it is checked at the renderer, not only at the API.
"""

import time

from smokemon import cli, hubapi, report, schema

NOW = 1_000_000.0


def _args(**kw):
    """A namespace shaped like the parsed argv the command handlers receive."""
    from types import SimpleNamespace
    base = dict(db=None, hub_url=None, hours=24.0, node=None, no_color=True, uid=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- unknown must never render as ongoing ----------

def test_unknown_and_ongoing_render_differently(seed):
    """Two incidents identical but for their node's liveness must not produce the same text."""
    seed.heartbeat("live01", NOW - 10)
    seed.heartbeat("gone01", NOW - 100_000)
    seed.incident("live01", "u-live", opened_ts=NOW - 5000)
    seed.incident("gone01", "u-gone", opened_ts=NOW - 5000)

    out = report.incidents_feed_report(
        hubapi.incidents_feed(seed.conn, hours=24, now=NOW), color=False)
    assert "unknown" in out
    assert "ongoing" in out
    # and the reason travels with it, so the reader learns WHY we do not know
    assert "node silent" in out


def test_unknown_state_carries_its_reason_and_a_distinct_colour():
    ongoing = report.incident_state({"state": "ongoing"}, color=True)
    unknown = report.incident_state({"state": "unknown", "unknown_reason": "node silent"},
                                    color=True)
    assert ongoing != unknown
    # distinct SGR codes: a reader skimming for red must not have `unknown` blend into `ongoing`
    assert report._INC_STATE_SGR["unknown"] != report._INC_STATE_SGR["ongoing"]
    assert "node silent" in unknown


def test_unknown_without_a_reason_still_says_so():
    """A hub that reported `unknown` with no reason must not render as a bare word an operator
    could mistake for a normal state."""
    assert "no close received" in report.incident_state({"state": "unknown"}, color=False)


# ---------- smoke incidents ----------

def test_incidents_command_prints_the_feed(seed, hub_db, capsys):
    seed.heartbeat("pi04", NOW - 10)
    seed.incident("pi04", "abc123", signal="disk.used_pct", entity="/", severity="error",
                  opened_ts=NOW - 600, closed_ts=NOW - 300, worst_value=94.0)
    # hours is huge so the fixture's fixed NOW falls inside the real-clock window
    assert cli._incidents(_args(db=str(hub_db), hours=1e6)) == 0
    out = capsys.readouterr().out
    assert "INCIDENTS" in out
    assert "disk.used_pct//" in out
    assert "pi04" in out


def test_incidents_command_filters_by_node(seed, hub_db, capsys):
    seed.heartbeat("pi04", NOW - 10)
    seed.heartbeat("pi09", NOW - 10)
    seed.incident("pi04", "u4", opened_ts=NOW - 600)
    seed.incident("pi09", "u9", signal="host.temp", entity="", opened_ts=NOW - 600)
    assert cli._incidents(_args(db=str(hub_db), hours=1e6, node="pi09")) == 0
    out = capsys.readouterr().out
    assert "pi09" in out and "pi04" not in out


def test_incidents_command_missing_db_exits_nonzero(tmp_path, capsys):
    assert cli._incidents(_args(db=str(tmp_path / "nope.db"))) == 1
    assert "no hub DB" in capsys.readouterr().err


def test_empty_feed_says_so_rather_than_printing_a_bare_header(hub_conn):
    out = report.incidents_feed_report(hubapi.incidents_feed(hub_conn, hours=24, now=NOW),
                                       color=False)
    assert "nothing recorded" in out


# ---------- smoke incident UID ----------

def test_incident_command_prints_samples_and_log_excerpt(seed, hub_db, capsys):
    seed.heartbeat("pi04", NOW - 10)
    seed.incident("pi04", "deadbeef01", opened_ts=NOW - 600, closed_ts=NOW - 300,
                  worst_value=77.0)
    schema.insert(seed.conn, "incident_samples",
                  [{"ts": NOW - 610 + i * 5, "uid": "deadbeef01",
                    "phase": "pre" if i < 2 else "during",
                    "signal": "ping.loss", "entity": "1.1.1.1", "value": float(i)}
                   for i in range(4)], node="pi04")
    schema.insert(seed.conn, "log_excerpts",
                  [{"ts": NOW - 599, "source": "syslog", "path": "/var/log/syslog",
                    "reason": "incident-open", "bytes": 40, "dropped": 0,
                    "excerpt": "eth0 link down", "uid": "deadbeef01"}], node="pi04")
    seed.conn.commit()

    assert cli._incident(_args(db=str(hub_db), uid="deadbeef01")) == 0
    out = capsys.readouterr().out
    assert "INCIDENT deadbeef01" in out
    assert "samples (4)" in out
    assert "pre" in out and "during" in out
    assert "eth0 link down" in out


def test_incident_command_unknown_uid_exits_nonzero(seed, hub_db, capsys):
    assert cli._incident(_args(db=str(hub_db), uid="nosuchuid")) == 1
    assert "no incident with uid" in capsys.readouterr().err


def test_incident_detail_reports_missing_evidence_rather_than_implying_none_existed(seed):
    """An incident whose samples have not shipped yet must not read as an incident that had
    nothing worth capturing -- there is no continuous series to fall back on."""
    seed.heartbeat("pi04", NOW - 10)
    seed.incident("pi04", "bare01", opened_ts=NOW - 600, closed_ts=NOW - 300)
    out = report.incident_detail_report(
        hubapi.incident_detail(seed.conn, "bare01", now=NOW), color=False)
    assert "none captured" in out


# ---------- fleet + density renderers ----------

def test_fleet_report_preserves_the_api_order(seed):
    """hubapi.fleet() sorts worst-first and that order encodes the dead/critical precedence,
    so the renderer must not re-sort into something friendlier like alphabetical."""
    seed.heartbeat("aaa-ok", NOW - 10)
    seed.heartbeat("zzz-dead", NOW - 100_000)
    rows = hubapi.fleet(seed.conn, now=NOW)
    out = report.fleet_report(rows, color=False)
    body = [ln for ln in out.split("\n") if "aaa-ok" in ln or "zzz-dead" in ln]
    assert "zzz-dead" in body[0] and "aaa-ok" in body[1]


def test_fleet_report_marks_open_incidents_it_cannot_vouch_for(seed):
    """An open count on a node we can no longer hear from is not evidence the fault persists."""
    seed.heartbeat("gone", NOW - 100_000)
    seed.incident("gone", "u1", opened_ts=NOW - 5000)
    out = report.fleet_report(hubapi.fleet(seed.conn, now=NOW), color=False)
    assert "1?" in out


def test_fleet_and_density_reports_handle_an_empty_fleet(hub_conn):
    assert "no nodes reporting yet" in report.fleet_report([], color=False)
    assert "no incidents" in report.density_report(
        hubapi.incident_density(hub_conn, 24, now=NOW), color=False)


def test_density_report_counts_every_hour_an_incident_spanned(seed):
    """A six-hour outage rendered as a single cell reads as a blip."""
    seed.incident("pi04", "long1", opened_ts=NOW - 5 * 3600, closed_ts=NOW - 600)
    out = report.density_report(hubapi.incident_density(seed.conn, 24, now=NOW), color=False)
    row = [ln for ln in out.split("\n") if ln.startswith("pi04")][0]
    assert row.count("1") >= 5


# ---------- both transports must feed the renderer the same shape ----------

def test_fleet_data_unwraps_the_http_envelope(monkeypatch, hub_db):
    """/api/fleet wraps the list for the dashboard; the DB path returns it bare. The renderer
    takes a list, so a regression here would only show up as a crash over --hub-url."""
    monkeypatch.setattr(cli, "_http_get_json",
                        lambda base, path: {"fleet": [{"node": "n1", "state": "ok"}]})
    data = cli._fleet_data(_args(hub_url="http://hub:8765", heatmap=False))
    assert data == [{"node": "n1", "state": "ok"}]


def test_incidents_command_uses_the_http_endpoint_when_given_a_hub_url(monkeypatch, capsys):
    seen = {}

    def fake(base, path):
        seen["path"] = path
        return {"since": 0, "until": 1, "incidents": [], "counts": {}}

    monkeypatch.setattr(cli, "_http_get_json", fake)
    assert cli._incidents(_args(hub_url="http://hub:8765", hours=6.0, node="pi04")) == 0
    assert seen["path"].startswith("/api/incidents?hours=6.0")
    assert "node=pi04" in seen["path"]


def test_incident_command_uses_the_http_endpoint_when_given_a_hub_url(monkeypatch, capsys):
    seen = []

    def fake(base, path):
        seen.append(path)
        if path.startswith("/api/fleet"):   # the silent-node reconciliation looks up liveness
            return {"fleet": [{"node": "n", "state": "ok", "liveness": "live"}]}
        return {"uid": "u1", "node": "n", "signal": "ping.loss", "entity": "",
                "severity": "warn", "opened_ts": NOW, "state": "ongoing",
                "transitions": [], "samples": [], "phases": {}, "evidence": []}

    monkeypatch.setattr(cli, "_http_get_json", fake)
    assert cli._incident(_args(hub_url="http://hub:8765", uid="u1")) == 0
    assert seen[0] == "/api/incident?uid=u1"


def test_fleet_heatmap_uses_the_density_endpoint(monkeypatch):
    seen = {}

    def fake(base, path):
        seen["path"] = path
        return {"hour0": 0, "buckets": 0, "counts": {}, "worst": {}, "nodes": []}

    monkeypatch.setattr(cli, "_http_get_json", fake)
    cli._fleet_data(_args(hub_url="http://hub:8765", heatmap=True, hours=168.0))
    assert seen["path"] == "/api/density?hours=168.0"


# ---------- argv routing ----------

def test_argv_routes_the_new_subcommands(monkeypatch):
    """`smoke incident` must not be swallowed by the bare-`smoke` default-to-status rule."""
    called = []
    monkeypatch.setattr(cli, "_incidents", lambda a: called.append(("incidents", a)) or 0)
    monkeypatch.setattr(cli, "_incident", lambda a: called.append(("incident", a)) or 0)
    monkeypatch.setattr("sys.argv", ["smoke", "incident", "abc123"])
    assert cli.main() == 0
    assert called[0][0] == "incident" and called[0][1].uid == "abc123"

    called.clear()
    monkeypatch.setattr("sys.argv", ["smoke", "incidents", "--hours", "48"])
    assert cli.main() == 0
    assert called[0][0] == "incidents" and called[0][1].hours == 48.0


def test_bare_smoke_still_defaults_to_status(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_text_report", lambda cmd, a: seen.append(cmd) or 0)
    monkeypatch.setattr("sys.argv", ["smoke", "--minutes", "30"])
    assert cli.main() == 0
    assert seen == ["status"]


def test_incident_detail_downgrades_ongoing_to_unknown_on_a_silent_node(seed, hub_db, capsys):
    """hubapi.incident_detail returns the raw reduction and does not apply the silent-node
    rule. The detail view is the one an operator reads before deciding whether to drive to
    site, so it must not be the one surface that still claims `ongoing`."""
    seed.heartbeat("gone", time.time() - 100_000)
    seed.incident("gone", "silent01", opened_ts=NOW - 5000)
    assert cli._incident(_args(db=str(hub_db), uid="silent01")) == 0
    out = capsys.readouterr().out
    assert "unknown" in out and "node silent" in out
    assert "ongoing" not in out


def test_incident_detail_keeps_ongoing_when_the_node_is_live(seed, hub_db, capsys):
    # a real-clock heartbeat: the liveness lookup runs against time.time(), not the fixture's NOW
    seed.heartbeat("here", time.time() - 10)
    seed.incident("here", "live01", opened_ts=NOW - 5000)
    assert cli._incident(_args(db=str(hub_db), uid="live01")) == 0
    out = capsys.readouterr().out
    assert "ongoing" in out and "unknown" not in out
