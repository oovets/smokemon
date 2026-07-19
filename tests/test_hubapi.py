"""The hub read layer: incident reduction, evidence joins, hub self-health, and Prometheus.

The tests that matter most here are the ones about what the hub refuses to claim. An open
incident is not proof a fault persists, and an hour with no rows is not proof it was fine.
"""

from pathlib import Path

from smokemon import hubapi, query, schema

NOW = 1_000_000.0


def _fresh(seed, node, now=NOW):
    """A heartbeat recent enough that the node counts as live."""
    seed.heartbeat(node, now - 10)


# ---------- node discovery ----------

def test_nodes_unions_every_table_a_node_can_appear_in(seed):
    """A node that only ever sent incidents is one whose heartbeat is broken -- the exact thing
    worth seeing -- so discovery must not key off heartbeats alone."""
    seed.heartbeat("hb-only", NOW - 10)
    seed.incident("inc-only", "u1", opened_ts=NOW - 100)
    seed.event("ev-only", NOW - 50)
    assert hubapi.nodes(seed.conn) == ["ev-only", "hb-only", "inc-only"]


def test_nodes_empty_db(hub_conn):
    assert hubapi.nodes(hub_conn) == []


# ---------- liveness ----------

def test_liveness_uses_the_nodes_own_interval():
    """Staleness comes from the interval carried in the row, so a node deliberately running a
    slower heartbeat is not declared dead by a hub-side constant it never heard of."""
    assert hubapi._liveness(None, 60.0) == "unknown"
    assert hubapi._liveness(30.0, 60.0) == "live"
    assert hubapi._liveness(5 * 60.0, 60.0) == "stale"      # past 3x interval
    assert hubapi._liveness(20 * 60.0, 60.0) == "dead"      # past 12x interval
    # the same absolute age against a slower heartbeat is still live
    assert hubapi._liveness(5 * 60.0, 600.0) == "live"


# ---------- incidents feed ----------

def test_open_incident_on_silent_node_reports_unknown_not_ongoing(seed):
    """The headline rule: the absence of a close transition is never read as "still broken".
    The node may have died mid-incident and will never send its close."""
    seed.heartbeat("gone", NOW - 100_000)
    seed.incident("gone", "u1", opened_ts=NOW - 5000)
    inc = hubapi.incidents_feed(seed.conn, hours=24, now=NOW)["incidents"][0]
    assert inc["state"] == "unknown"
    assert inc["unknown_reason"] == "node silent"


def test_open_incident_on_live_node_reports_ongoing(seed):
    """The converse: with a fresh heartbeat the hub is entitled to say the fault persists."""
    _fresh(seed, "live01")
    seed.incident("live01", "u1", opened_ts=NOW - 5000)
    inc = hubapi.incidents_feed(seed.conn, hours=24, now=NOW)["incidents"][0]
    assert inc["state"] == "ongoing"
    assert "unknown_reason" not in inc


def test_closed_incident_stays_closed_on_a_silent_node(seed):
    """A close that did arrive is a fact, and losing the node afterwards does not unmake it."""
    seed.heartbeat("gone", NOW - 100_000)
    seed.incident("gone", "u1", opened_ts=NOW - 5000, closed_ts=NOW - 4000)
    inc = hubapi.incidents_feed(seed.conn, hours=24, now=NOW)["incidents"][0]
    assert inc["state"] == "closed"


def test_feed_counts_and_severity_gate(seed):
    _fresh(seed, "n1")
    seed.incident("n1", "warn1", severity="warn", opened_ts=NOW - 100)
    seed.incident("n1", "crit1", severity="crit", opened_ts=NOW - 200)
    seed.incident("n1", "info1", severity="info", opened_ts=NOW - 300)

    everything = hubapi.incidents_feed(seed.conn, hours=24, min_severity=1, now=NOW)
    assert everything["counts"]["total"] == 3 and everything["counts"]["ongoing"] == 3

    elevated = hubapi.incidents_feed(seed.conn, hours=24, min_severity=3, now=NOW)
    assert {i["uid"] for i in elevated["incidents"]} == {"crit1"}


def test_feed_limit_sets_truncated(seed):
    _fresh(seed, "n1")
    for i in range(10):
        seed.incident("n1", f"u{i}", opened_ts=NOW - i * 10)
    out = hubapi.incidents_feed(seed.conn, hours=24, limit=4, now=NOW)
    assert len(out["incidents"]) == 4 and out["truncated"] is True
    assert hubapi.incidents_feed(seed.conn, hours=24, limit=50, now=NOW)["truncated"] is False


def test_feed_node_filter(seed):
    _fresh(seed, "n1")
    _fresh(seed, "n2")
    seed.incident("n1", "a", opened_ts=NOW - 100)
    seed.incident("n2", "b", opened_ts=NOW - 100)
    out = hubapi.incidents_feed(seed.conn, hours=24, node="n2", now=NOW)
    assert [i["uid"] for i in out["incidents"]] == ["b"]


# ---------- incident detail ----------

def test_incident_detail_returns_none_for_unknown_uid(hub_conn):
    assert hubapi.incident_detail(hub_conn, "nope", now=NOW) is None


def test_incident_detail_groups_samples_by_phase_and_joins_evidence(seed):
    _fresh(seed, "n1")
    seed.incident("n1", "u1", opened_ts=NOW - 500, closed_ts=NOW - 100)
    schema.insert(seed.conn, "incident_samples", [
        {"ts": NOW - 600, "uid": "u1", "phase": "pre", "signal": "ping.loss", "value": 0.0},
        {"ts": NOW - 400, "uid": "u1", "phase": "during", "signal": "ping.loss", "value": 90.0},
        {"ts": NOW - 90, "uid": "u1", "phase": "post", "signal": "ping.loss", "value": 0.0},
    ], node="n1")
    schema.insert(seed.conn, "log_excerpts", [
        {"ts": NOW - 400, "uid": "u1", "source": "syslog", "path": "/var/log/syslog",
         "reason": "incident", "bytes": 5, "dropped": 0, "excerpt": "uh oh"},
    ], node="n1")
    seed.conn.commit()

    d = hubapi.incident_detail(seed.conn, "u1", now=NOW)
    assert [s["phase"] for s in d["samples"]] == ["pre", "during", "post"]
    assert len(d["phases"]["pre"]) == 1 and d["phases"]["during"][0]["value"] == 90.0
    assert d["evidence"][0]["excerpt"] == "uh oh"


def test_incident_samples_are_visible_before_their_parent_arrives(seed):
    """Samples ship ahead of their transition row. A join would hide exactly the rows that
    prove what happened, so orphans stay readable and surface as hub self-health instead."""
    schema.insert(seed.conn, "incident_samples",
                  [{"ts": NOW - 100, "uid": "orphan", "phase": "during",
                    "signal": "ping.loss", "value": 50.0}], node="n1")
    seed.conn.commit()
    orphans, oldest = query.orphan_stats(seed.conn, NOW)
    assert orphans == 1 and oldest == 100
    assert hubapi.hub_health(seed.conn, NOW)["orphan_samples"] == 1


# ---------- density ----------

def test_density_counts_an_incident_in_every_hour_it_spanned(seed):
    """A six-hour outage rendered as a single cell reads as a blip, so the incident occupies
    every hour between open and close -- not only the one it opened in."""
    now = 100 * 3600.0                        # hour-aligned, keeps the arithmetic legible
    seed.incident("n1", "u1", opened_ts=now - 6 * 3600, closed_ts=now - 1 * 3600)
    row = hubapi.incident_density(seed.conn, hours=12, now=now)["counts"]["n1"]
    assert sum(1 for c in row if c) == 6       # the six hours it was open, inclusive
    assert row[-1] == 0                        # the hour after the close is clean


def test_density_still_open_incident_runs_to_now(seed):
    now = 100 * 3600.0
    seed.incident("n1", "u1", opened_ts=now - 3 * 3600)    # never closed
    row = hubapi.incident_density(seed.conn, hours=12, now=now)["counts"]["n1"]
    assert row[-1] == 1                        # counted right up to the current hour


def test_density_tracks_worst_severity_per_cell(seed):
    now = 100 * 3600.0
    seed.incident("n1", "warn", severity="warn", opened_ts=now - 1800)
    seed.incident("n1", "crit", severity="crit", opened_ts=now - 1800)
    d = hubapi.incident_density(seed.conn, hours=12, now=now)
    assert d["counts"]["n1"][-1] == 2 and d["worst"]["n1"][-1] == 4


def test_density_empty_cell_means_nothing_happened(seed):
    """The old loss heatmap could not distinguish "no data" from "fine". Counting incidents
    inverts that: a node with nothing wrong simply has no row."""
    _fresh(seed, "quiet")
    d = hubapi.incident_density(seed.conn, hours=12, now=NOW)
    assert d["counts"] == {} and d["nodes"] == []


# ---------- events log ----------

def test_events_log_severity_filter_is_applied_in_sql(seed):
    """Regression: the elevated filter used to run in Python AFTER the LIMIT, so when the fleet
    flapped hardest the newest rows were all recovery noise and the view came back empty --
    blank exactly when it mattered. 300 info rows must not push the one warn row off the end."""
    for i in range(300):
        seed.event("n1", NOW - 3000 + i, severity="info", ev=f"noise{i}")
    seed.event("n1", NOW - 3500, severity="warn", ev="the-real-one")   # the OLDEST row

    out = hubapi.events_log(seed.conn, severity="elevated", hours=24, limit=200, now=NOW)
    assert [e["event"] for e in out["events"]] == ["the-real-one"]

    everything = hubapi.events_log(seed.conn, severity="all", hours=24, limit=200, now=NOW)
    assert len(everything["events"]) == 200     # unfiltered, the limit really does bite


def test_events_log_treats_unknown_severity_as_elevated(seed):
    """An unrecognised severity is more likely a new elevated level than something safe to
    hide, so it survives the quiet-list filter."""
    seed.event("n1", NOW - 100, severity="emergency", ev="odd")
    out = hubapi.events_log(seed.conn, severity="elevated", hours=24, now=NOW)
    assert [e["event"] for e in out["events"]] == ["odd"]


def test_events_log_node_filter_and_excerpts(seed):
    seed.event("n1", NOW - 100, severity="error", ev="mine")
    seed.event("n2", NOW - 100, severity="error", ev="theirs")
    schema.insert(seed.conn, "log_excerpts",
                  [{"ts": NOW - 100, "source": "syslog", "path": "/p", "reason": "r",
                    "bytes": 1, "dropped": 0, "excerpt": "x", "uid": None}], node="n1")
    seed.conn.commit()
    out = hubapi.events_log(seed.conn, node="n1", hours=24, now=NOW)
    assert [e["event"] for e in out["events"]] == ["mine"]
    assert [x["node"] for x in out["excerpts"]] == ["n1"]


# ---------- open_incident_alerts ----------

def test_open_incident_alerts_excludes_silent_nodes(seed):
    """Paging about a fault we are no longer receiving updates for produces an alert that can
    never clear, because the close that would resolve it has nowhere to go."""
    _fresh(seed, "live01")
    seed.heartbeat("silent01", NOW - 100_000)
    seed.incident("live01", "u-live", opened_ts=NOW - 500)
    seed.incident("silent01", "u-silent", opened_ts=NOW - 500)
    assert set(hubapi.open_incident_alerts(seed.conn, NOW)) == {"u-live"}


def test_open_incident_alerts_excludes_closed_and_is_keyed_by_uid(seed):
    _fresh(seed, "n1")
    seed.incident("n1", "u-open", entity="1.1.1.1", severity="crit",
                  opened_ts=NOW - 500, worst_value=93.5)
    seed.incident("n1", "u-done", opened_ts=NOW - 900, closed_ts=NOW - 800)
    out = hubapi.open_incident_alerts(seed.conn, NOW)
    assert set(out) == {"u-open"}
    a = out["u-open"]
    assert a["key"] == "u-open" and a["node"] == "n1" and a["severity"] == 4
    assert a["kind"] == "ping.loss" and a["label"] == "1.1.1.1"
    assert "worst 93.5" in a["detail"]


# ---------- hub self-health ----------

def test_hub_health_reports_row_counts_and_no_orphans(seed):
    _fresh(seed, "n1")
    seed.incident("n1", "u1", opened_ts=NOW - 100)
    h = hubapi.hub_health(seed.conn, NOW)
    assert h["orphan_samples"] == 0 and h["oldest_orphan_s"] == 0.0
    assert h["rows"]["incidents"] == 1 and h["rows"]["heartbeats"] == 1


# ---------- prometheus ----------

def test_prometheus_exports_liveness_and_open_incidents(seed):
    _fresh(seed, "n1")
    seed.incident("n1", "u1", opened_ts=NOW - 100)
    text = hubapi.prometheus(seed.conn, NOW)
    assert 'smokemon_node_live{node="n1"} 1' in text
    assert 'smokemon_open_incidents{node="n1"} 1' in text
    assert "smokemon_heartbeat_age_seconds" in text
    assert "smokemon_orphan_samples 0" in text


def test_prometheus_exports_no_per_signal_gauges(seed):
    """Synthesising a time series from incident windows would export a chart made only of the
    bad moments -- worse than exporting nothing, because it would look complete."""
    _fresh(seed, "n1")
    seed.incident("n1", "u1", signal="ping.loss", opened_ts=NOW - 100, worst_value=90.0)
    text = hubapi.prometheus(seed.conn, NOW)
    assert "ping" not in text and "loss" not in text


def test_prometheus_escapes_label_values(seed):
    seed.heartbeat('weird"node\\x', NOW - 10)
    assert 'node="weird\\"node\\\\x"' in hubapi.prometheus(seed.conn, NOW)


def test_prometheus_on_empty_db_is_valid_not_a_crash(hub_conn):
    """No nodes means no per-node gauges at all, but the hub's own orphan counter still
    reports -- a scrape of an empty hub is a valid scrape, not an error."""
    text = hubapi.prometheus(hub_conn, NOW)
    assert "smokemon_orphan_samples 0" in text
    assert "smokemon_node_live" not in text
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert len(line.rsplit(" ", 1)) == 2


# ---------- dashboard asset ----------

def test_dashboard_html_is_read_from_the_package():
    html = hubapi.dashboard_html()
    assert html.lstrip().lower().startswith("<!doctype html")
    # Not the fallback, which is also valid HTML -- that is what makes a packaging mistake
    # look like a working page until you read the words on it.
    assert "missing from this install" not in html
    assert "/api/incidents" in html


def test_dashboard_html_missing_asset_degrades_to_a_message(monkeypatch):
    """A missing asset must not 500 the hub's only human-facing page."""
    from importlib import resources

    def boom(*_a, **_k):
        raise OSError("gone")
    monkeypatch.setattr(resources, "files", boom)
    assert "dashboard.html is missing" in hubapi.dashboard_html()


def test_favicon_is_an_svg():
    assert hubapi.FAVICON_SVG.startswith(b"<svg") and b"#58a6ff" in hubapi.FAVICON_SVG


def test_esc_escapes_html():
    assert hubapi.esc('<a href="x">') == "&lt;a href=&quot;x&quot;&gt;"
    assert hubapi.esc(None) == ""


def test_dashboard_is_served_from_disk_and_is_packaged():
    """The dashboard used to be a 1612-line string constant inside hubapi.py -- 59% of the
    module, unlintable, and testable only by asserting substrings. It is a file now, so this
    checks the two things that can actually break: that it is present, and that packaging
    ships it (without package-data an installed hub serves the missing-file fallback)."""
    import tomllib

    asset = Path(hubapi.__file__).with_name("static") / "dashboard.html"
    assert asset.exists(), "dashboard.html is missing from the source tree"
    html = hubapi.dashboard_html()
    assert "missing from this install" not in html
    assert html.lstrip().startswith("<!doctype html>")

    pyproject = Path(hubapi.__file__).parent.parent / "pyproject.toml"
    cfg = tomllib.loads(pyproject.read_text())
    data = cfg["tool"]["setuptools"].get("package-data", {}).get("smokemon", [])
    assert any(p.startswith("static") for p in data), \
        "static assets are not declared as package-data; the wheel would omit the dashboard"


def test_dashboard_references_only_endpoints_that_exist():
    """A renamed endpoint leaves the page silently blank, which is the failure mode a
    dashboard is least likely to survive unnoticed."""
    import re

    from smokemon import hub

    html = hubapi.dashboard_html()
    used = set(re.findall(r'["`/](/api/[a-z-]+)', html))
    served = set(re.findall(r'u\.path == "(/api/[a-z-]+)"', Path(hub.__file__).read_text()))
    assert used, "no API calls found in the dashboard -- the regex probably stopped matching"
    assert used <= served, f"dashboard calls endpoints the hub does not serve: {used - served}"


def test_incident_detail_applies_the_silent_node_rule(hub_conn, seed):
    """The detail view is reached directly by uid, so it must reach the same conclusion as the
    feed. One incident reading 'ongoing' on its own page and 'unknown' in the list would leave
    the more emphatic of the two being the guess."""
    now = 1_000_000.0
    # No heartbeat at all: the node has never checked in, so its liveness is unknown and an
    # open incident on it cannot be claimed to be ongoing.
    seed.incident("ghost", "deadbeefdeadbeef", opened_ts=now - 3600)
    d = hubapi.incident_detail(hub_conn, "deadbeefdeadbeef", now=now)
    assert d["state"] == "unknown"
    assert d.get("unknown_reason") == "node silent"

    feed = hubapi.incidents_feed(hub_conn, hours=24, now=now)["incidents"]
    same = [i for i in feed if i["uid"] == "deadbeefdeadbeef"][0]
    assert same["state"] == d["state"], "feed and detail disagree about the same incident"
