"""fleet(): one row per node, liveness from the heartbeat and health from open incidents.

The distinction these tests defend is `state` keeping "we know it is broken" separate from "we
have stopped hearing from it". Collapsing those is how a monitoring system ends up reporting
that everything is fine while a node is powered off.
"""

from smokemon import hubapi

NOW = 1_000_000.0
IV = 300.0   # the heartbeat interval the seed fixture writes


def _state(conn, node, now=NOW):
    return next(r for r in hubapi.fleet(conn, now) if r["node"] == node)


# ---------- liveness beats health ----------

def test_dead_and_unknown_are_not_reported_as_degraded(seed):
    """A node past the dead threshold reports `dead` even with incidents open, and a node that
    has never sent a heartbeat reports `unknown`. Neither may be dressed up as a health verdict:
    the open incidents on them are not evidence the fault persists."""
    seed.heartbeat("dead01", NOW - IV * 20)
    seed.incident("dead01", "u-dead", severity="crit", opened_ts=NOW - 5000)
    seed.incident("never01", "u-unknown", severity="crit", opened_ts=NOW - 5000)

    dead = _state(seed.conn, "dead01")
    assert dead["state"] == "dead" and dead["liveness"] == "dead"
    unknown = _state(seed.conn, "never01")
    assert unknown["state"] == "unknown" and unknown["liveness"] == "unknown"
    assert unknown["age_s"] is None

    # ...and neither claims its open incident is trustworthy
    assert dead["open_trustworthy"] is False and unknown["open_trustworthy"] is False


def test_degraded_and_critical_come_from_open_incident_severity(seed):
    """A live node with open incidents is a health verdict, and the worst severity picks which
    one: crit escalates to `critical`, anything lower is `degraded`."""
    seed.heartbeat("warn01", NOW - 10)
    seed.incident("warn01", "u-warn", severity="warn", opened_ts=NOW - 500)
    seed.heartbeat("crit01", NOW - 10)
    seed.incident("crit01", "u-crit", severity="crit", opened_ts=NOW - 500)

    warn = _state(seed.conn, "warn01")
    assert warn["state"] == "degraded" and warn["worst_severity"] == 2
    crit = _state(seed.conn, "crit01")
    assert crit["state"] == "critical" and crit["worst_severity"] == 4
    assert warn["open_trustworthy"] is True and crit["open_trustworthy"] is True


def test_stale_is_its_own_state_between_live_and_dead(seed):
    seed.heartbeat("stale01", NOW - IV * 5)
    r = _state(seed.conn, "stale01")
    assert r["state"] == "stale" and r["liveness"] == "stale"
    assert r["open_trustworthy"] is False      # not live -> its open incidents are not facts


def test_healthy_node_is_ok(seed):
    seed.heartbeat("ok01", NOW - 10)
    r = _state(seed.conn, "ok01")
    assert r["state"] == "ok" and r["liveness"] == "live"
    assert r["open_incidents"] == 0 and r["worst_severity"] == 0
    assert r["age_s"] == 10.0 and r["heartbeat"]["interval_s"] == IV


def test_closed_incidents_do_not_degrade_a_node(seed):
    """Only currently-open incidents count toward health; history does not keep a node red."""
    seed.heartbeat("ok01", NOW - 10)
    seed.incident("ok01", "u-old", severity="crit", opened_ts=NOW - 5000, closed_ts=NOW - 4000)
    r = _state(seed.conn, "ok01")
    assert r["state"] == "ok" and r["open_incidents"] == 0


# ---------- ordering ----------

def test_worst_first_ordering(seed):
    """The grid is read top-down under pressure, so the states that need a human come first."""
    seed.heartbeat("ok01", NOW - 10)
    seed.heartbeat("stale01", NOW - IV * 5)
    seed.heartbeat("dead01", NOW - IV * 20)
    seed.heartbeat("crit01", NOW - 10)
    seed.incident("crit01", "u-crit", severity="crit", opened_ts=NOW - 100)
    seed.heartbeat("warn01", NOW - 10)
    seed.incident("warn01", "u-warn", severity="warn", opened_ts=NOW - 100)
    seed.incident("never01", "u-unknown", opened_ts=NOW - 100)

    assert [r["node"] for r in hubapi.fleet(seed.conn, NOW)] == [
        "dead01", "never01", "crit01", "warn01", "stale01", "ok01"]


def test_fleet_of_an_empty_hub_is_empty(hub_conn):
    assert hubapi.fleet(hub_conn, NOW) == []
