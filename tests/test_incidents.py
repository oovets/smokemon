"""Incident persistence: uid identity, reopen policy, decimation, restart recovery.

Where test_detect.py drives the state machine with no database, this drives the whole path
into SQLite and checks what actually lands on disk -- above all that normal operation lands
nothing at all.
"""

import pytest

from smokemon import baseline, config, core, detect, incidents, schema, signals


@pytest.fixture(autouse=True)
def _clean():
    signals.reset()
    baseline.reset()
    detect.reset()
    detect.reload_rules("")
    yield
    signals.reset()
    baseline.reset()
    detect.reset()


@pytest.fixture()
def conn(tmp_db):
    c = core.connect(str(tmp_db))
    schema.init_node(c)
    incidents.ensure_table(c)
    baseline.ensure_table(c)
    yield c
    c.close()


class Feeder:
    def __init__(self, conn, signal="ping.loss", entity="1.1.1.1", t0=1_000_000.0):
        self.conn, self.signal, self.entity = conn, signal, entity
        self.wall, self.mono = t0, 5_000.0

    def at(self, dt, value):
        self.wall += dt
        self.mono += dt
        acts = detect.evaluate(self.signal, self.entity, value, self.wall, self.mono)
        incidents.apply(self.conn, acts)
        return acts

    def hold(self, seconds, value, step=10.0):
        for _ in range(max(1, int(seconds // step))):
            self.at(step, value)

    def open_one(self):
        """Drive a ping.loss incident to OPEN (trip=10, for_s=20)."""
        for _ in range(8):
            self.at(10, 0.0)
        self.at(10, 50.0)
        self.at(25, 50.0)


def rows(conn, table, cols="*"):
    # incident_state is keyed by `key`, not a rowid, so it has no id to order by.
    order = "" if table == "incident_state" else " ORDER BY id"
    return conn.execute(f"SELECT {cols} FROM {table}{order}").fetchall()


# ---------- invariant 1: normal operation writes nothing ----------

def test_healthy_operation_writes_no_rows(conn):
    """The headline claim of the pivot. If this ever fails, the design is not delivering."""
    f = Feeder(conn)
    for _ in range(200):
        f.at(10, 0.0)
    assert rows(conn, "incidents") == []
    assert rows(conn, "incident_samples") == []
    assert rows(conn, "incident_state") == []


def test_sub_debounce_flap_writes_no_rows(conn):
    """ARMED must never reach disk -- otherwise debounce is just another kind of noise."""
    f = Feeder(conn)
    for _ in range(20):
        f.at(10, 0.0)
        f.at(10, 90.0)      # breach, then gone well before for_s=20
    assert rows(conn, "incidents") == []
    assert rows(conn, "incident_samples") == []


# ---------- opening ----------

def test_open_writes_transition_plus_pre_and_onset(conn):
    f = Feeder(conn)
    f.open_one()
    inc = rows(conn, "incidents", "uid, transition, signal, severity, value, threshold")
    assert len(inc) == 1
    uid, transition, signal, severity, value, threshold = inc[0]
    assert transition == "open" and signal == "ping.loss"
    assert severity == "error" and value == 50.0 and threshold == 10.0

    phases = conn.execute(
        "SELECT phase, value FROM incident_samples WHERE uid=? ORDER BY id", (uid,)).fetchall()
    pre = [v for p, v in phases if p == "pre"]
    during = [v for p, v in phases if p == "during"]
    assert pre and all(v == 0.0 for v in pre), "pre must be the healthy baseline only"
    assert during and all(v == 50.0 for v in during), "onset must be the breach coming on"


def test_open_carries_rule_provenance(conn):
    """An incident must stay interpretable after a rule change: the raw data that would let
    you re-derive the threshold no longer exists."""
    f = Feeder(conn)
    f.open_one()
    row = conn.execute("SELECT rule, rule_hash, detector_version, schema_version, "
                       "peak_mode, comparison_direction FROM incidents").fetchone()
    rule, rhash, dver, sver, peak_mode, direction = row
    assert rule == "ping.loss" and rhash and dver == detect.DETECTOR_VERSION
    assert sver == incidents.SCHEMA_VERSION
    assert peak_mode == "max" and direction == "+"


def test_open_emits_an_expeditable_event(conn):
    """The incident reaches the hub through the existing coalesced expedite path. source must
    not be 'collector', which expedite deliberately skips to avoid a feedback loop."""
    f = Feeder(conn)
    f.open_one()
    ev = conn.execute("SELECT source, severity, event FROM ext_events").fetchall()
    assert ev, "no event emitted; the incident would wait for the bulk ship tick"
    source, severity, event = ev[-1]
    assert source == "ping" and source != "collector"
    assert event == "incident-open" and severity == "error"


# ---------- reopen policy ----------

def _close(f):
    f.hold(80, 0.0)


def test_retrip_inside_reopen_window_keeps_the_same_uid(conn, monkeypatch):
    """A link flapping every few minutes is ONE incident, not a dozen."""
    monkeypatch.setattr(config, "INCIDENT_REOPEN_WINDOW_S", 900.0)
    f = Feeder(conn)
    f.open_one()
    _close(f)
    f.at(10, 50.0)                     # re-trip well inside the window
    got = rows(conn, "incidents", "uid, transition")
    uids = {u for u, _t in got}
    assert len(uids) == 1, "a re-trip inside the window minted a second incident"
    assert [t for _u, t in got] == ["open", "close", "reopen"]


def test_retrip_outside_reopen_window_mints_a_new_uid(conn, monkeypatch):
    """A morning outage and an evening outage are two incidents, however similar."""
    monkeypatch.setattr(config, "INCIDENT_REOPEN_WINDOW_S", 60.0)
    f = Feeder(conn)
    f.open_one()
    _close(f)
    f.hold(600, 0.0)                   # let the window lapse (cooldown also lapses)
    f.at(10, 50.0)
    f.at(25, 50.0)
    uids = {u for (u,) in conn.execute("SELECT DISTINCT uid FROM incidents")}
    assert len(uids) == 2, "two separated outages collapsed into one incident"


def test_reopen_window_is_independent_of_cooldown(conn, monkeypatch):
    """They answer different questions: cooldown is when the detector may trip again,
    the reopen window is whether it counts as the same occurrence."""
    monkeypatch.setattr(config, "INCIDENT_REOPEN_WINDOW_S", 5.0)
    f = Feeder(conn)
    f.open_one()
    _close(f)
    f.at(10, 50.0)                     # inside cooldown (300s) but outside reopen window (5s)
    transitions = [t for (t,) in conn.execute("SELECT transition FROM incidents ORDER BY id")]
    assert transitions == ["open", "close", "open"], transitions


# ---------- decimation ----------

def test_during_samples_are_hard_capped(conn):
    """Cost per incident must be bounded regardless of duration: a three-day outage should
    cost about the same as a three-minute one."""
    f = Feeder(conn)
    f.open_one()
    f.hold(30_000, 50.0)               # a very long incident
    n = conn.execute("SELECT COUNT(*) FROM incident_samples WHERE phase='during'").fetchone()[0]
    assert n <= config.INCIDENT_DURING_MAX, f"{n} during-samples exceeds the cap"


def test_total_rows_per_incident_are_bounded(conn):
    f = Feeder(conn)
    f.open_one()
    f.hold(30_000, 50.0)
    _close(f)
    total = conn.execute("SELECT COUNT(*) FROM incident_samples").fetchone()[0]
    ceiling = (config.INCIDENT_PRE_SAMPLES + config.INCIDENT_DURING_MAX
               + config.INCIDENT_POST_SAMPLES)
    assert total <= ceiling, f"{total} sample rows exceeds the stated ceiling of {ceiling}"


def test_close_records_duration_and_worst_value(conn):
    f = Feeder(conn)
    f.open_one()
    f.at(10, 90.0)                     # the worst moment
    f.at(10, 40.0)
    _close(f)
    row = conn.execute("SELECT transition, duration_s, worst_value, n_samples "
                       "FROM incidents WHERE transition='close'").fetchone()
    assert row is not None
    _t, duration, worst, n_samples = row
    assert duration and duration > 0
    assert worst == 90.0, "worst_value must be the peak, not the last value"
    assert n_samples is not None


def test_close_captures_a_recovery_tail(conn):
    f = Feeder(conn)
    f.open_one()
    _close(f)
    post = conn.execute(
        "SELECT COUNT(*) FROM incident_samples WHERE phase='post'").fetchone()[0]
    assert 0 < post <= config.INCIDENT_POST_SAMPLES


# ---------- storm budget ----------

def test_beyond_max_open_incidents_record_transitions_only(conn, monkeypatch):
    """Degrade detail, never detection. A node in a storm is exactly when we must not stop
    noticing things."""
    monkeypatch.setattr(config, "INCIDENT_MAX_OPEN", 2)
    for i in range(5):
        f = Feeder(conn, entity=f"target-{i}")
        f.open_one()
    opened = conn.execute("SELECT COUNT(*) FROM incidents WHERE transition='open'").fetchone()[0]
    assert opened == 5, "detection stopped under load"
    with_samples = conn.execute(
        "SELECT COUNT(DISTINCT uid) FROM incident_samples").fetchone()[0]
    assert with_samples <= 2, "evidence budget was not enforced"


# ---------- restart recovery ----------

def test_restart_resumes_the_same_incident(conn):
    """A still-broken condition must RESUME, not open a second incident -- the hub should see
    one incident spanning the restart. events.py's in-memory state cannot do this, which is
    why detect is a separate durable path rather than an extension of it."""
    f = Feeder(conn)
    f.open_one()
    uid_before = conn.execute("SELECT uid FROM incidents").fetchone()[0]

    detect.reset()                     # simulate process restart (in-memory state lost)
    restored = incidents.load_open(conn, now_mono=f.mono, now_wall=f.wall)
    assert restored == 1

    f.at(10, 50.0)                     # still broken
    f.hold(80, 0.0)                    # and recovers
    uids = {u for (u,) in conn.execute("SELECT DISTINCT uid FROM incidents")}
    assert uids == {uid_before}, "restart split one incident into two"
    transitions = [t for (t,) in conn.execute("SELECT transition FROM incidents ORDER BY id")]
    assert transitions == ["open", "close"]


def test_restart_while_armed_forgets_the_candidate(conn):
    """Invariant 2. An unconfirmed anomaly does not survive a restart; its debounce starts
    over. That is the accepted cost of refusing to write candidates to disk."""
    f = Feeder(conn)
    for _ in range(5):
        f.at(10, 0.0)
    f.at(10, 50.0)                     # ARMED, nothing persisted
    detect.reset()
    assert incidents.load_open(conn) == 0
    assert rows(conn, "incidents") == []


def test_restart_in_cooldown_preserves_the_reopen_window(conn, monkeypatch):
    monkeypatch.setattr(config, "INCIDENT_REOPEN_WINDOW_S", 900.0)
    f = Feeder(conn)
    f.open_one()
    _close(f)
    detect.reset()
    incidents.load_open(conn, now_mono=f.mono, now_wall=f.wall)
    f.at(10, 50.0)
    uids = {u for (u,) in conn.execute("SELECT DISTINCT uid FROM incidents")}
    assert len(uids) == 1, "the reopen window did not survive a restart"


def test_active_uid_reports_the_open_incident(conn):
    """logexcerpt stamps evidence with this so a captured log tail is linked to the incident
    that triggered it."""
    assert incidents.active_uid(conn) is None
    f = Feeder(conn)
    f.open_one()
    uid = conn.execute("SELECT uid FROM incidents").fetchone()[0]
    assert incidents.active_uid(conn) == uid
    _close(f)
    assert incidents.active_uid(conn) is None


# ---------- baseline persistence ----------

def test_baseline_survives_a_restart(conn):
    f = Feeder(conn, signal="host.temp", entity="cpu")
    for _ in range(50):
        f.at(30, 42.0)
    baseline.maybe_flush(conn, now=f.wall, force=True)
    learned = baseline.get("host.temp", "cpu").center

    baseline.reset()
    baseline.load(conn)
    assert baseline.get("host.temp", "cpu").center == pytest.approx(learned)
