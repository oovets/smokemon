"""The detector state machine, driven against synthetic sample sequences.

These tests carry the invariants from the incident-storage pivot. detect.evaluate() returns
declarative actions and never touches SQLite, so the whole machine is exercised here with no
database at all -- which is the point of that boundary.

Time is supplied explicitly (wall and monotonic separately) so debounce, hysteresis holds and
cooldown are tested deterministically rather than by sleeping.
"""

import pytest

from smokemon import baseline, config, detect, signals


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


class Driver:
    """Feeds a signal with controllable wall/monotonic clocks."""

    def __init__(self, signal="ping.loss", entity="1.1.1.1", t0=1_000_000.0):
        self.signal, self.entity = signal, entity
        self.wall = t0
        self.mono = 5_000.0
        self.acts = []

    def at(self, dt, value):
        """Advance both clocks by dt and feed one sample. Returns actions from that sample."""
        self.wall += dt
        self.mono += dt
        got = detect.evaluate(self.signal, self.entity, value, self.wall, self.mono)
        self.acts += got
        return got

    def hold(self, seconds, value, step=10.0):
        """Feed `value` every `step` seconds for `seconds`."""
        out = []
        n = max(1, int(seconds // step))
        for _ in range(n):
            out += self.at(step, value)
        return out

    def ops(self):
        return [a.op for a in self.acts]


# ---------- debounce / flap filtering ----------

def test_brief_breach_under_for_s_writes_nothing():
    """A spike shorter than for_s is a hypothesis, not an incident. ARMED must never reach
    disk -- otherwise debounce just becomes another kind of noise."""
    d = Driver()                      # ping.loss: trip=10, for_s=20
    d.at(10, 0.0)
    d.at(10, 55.0)                    # breach -> ARMED
    assert d.acts == []
    d.at(10, 0.0)                     # recovered before for_s elapsed
    assert d.acts == [], "a sub-debounce spike must produce no actions at all"
    assert detect.state_of(detect.incident_key("ping.loss", "1.1.1.1")) == detect.OK


def test_sustained_breach_opens_after_for_s():
    d = Driver()
    d.at(10, 0.0)
    assert d.at(10, 55.0) == []       # ARMED
    acts = d.at(15, 60.0)             # 15s > for_s=20? no -- still armed at t+15
    assert acts == []
    acts = d.at(10, 60.0)             # now 25s of sustained breach
    assert [a.op for a in acts] == ["open"]
    assert acts[0].value == 60.0
    assert acts[0].threshold == 10.0


def test_open_carries_pre_window_from_the_ring():
    """The baseline-before window comes out of memory at trip time, so it costs no disk while
    things are healthy."""
    d = Driver()
    for _ in range(8):
        d.at(10, 0.0)
    d.at(10, 50.0)
    acts = d.at(25, 50.0)
    assert acts[0].op == "open"
    pre = acts[0].pre
    assert 0 < len(pre) <= config.INCIDENT_PRE_SAMPLES
    assert all(v == 0.0 for _ts, v in pre), "pre-window must be the healthy samples, not the breach"


# ---------- hysteresis ----------

def test_bounce_in_closing_stays_one_incident():
    """The flap absorber. A signal oscillating around the band must not produce N incidents."""
    d = Driver()
    d.at(10, 0.0)
    d.at(10, 50.0)
    d.at(25, 50.0)                    # open
    assert d.ops().count("open") == 1
    for _ in range(6):
        d.at(10, 0.5)                 # below clear -> CLOSING
        d.at(10, 50.0)                # breach again -> back to OPEN, no new incident
    assert d.ops().count("open") == 1, "a bouncing signal must be one incident"
    assert d.ops().count("close") == 0


def test_value_inside_hysteresis_band_neither_closes_nor_reopens():
    """Between clear=1 and trip=10 the signal is neither bad enough to trip nor good enough
    to believe. It must not close on that."""
    d = Driver()
    d.at(10, 0.0); d.at(10, 50.0); d.at(25, 50.0)
    d.hold(300, 5.0)                  # in the band for well past clear_for_s=60
    assert d.ops().count("close") == 0


def test_close_requires_clear_for_s():
    d = Driver()
    d.at(10, 0.0); d.at(10, 50.0); d.at(25, 50.0)
    d.at(10, 0.0)                     # clearing begins
    assert d.ops().count("close") == 0
    d.hold(70, 0.0)                   # hold recovery past clear_for_s=60
    assert d.ops().count("close") == 1


# ---------- cooldown ----------

def test_retrip_during_cooldown_reopens_without_new_debounce():
    d = Driver()
    d.at(10, 0.0); d.at(10, 50.0); d.at(25, 50.0)
    d.hold(80, 0.0)                   # close -> COOLDOWN (cooldown_s=300)
    assert d.ops().count("close") == 1
    acts = d.at(10, 50.0)             # breach inside cooldown
    assert [a.op for a in acts] == ["open"]


def test_cooldown_expires_back_to_ok():
    d = Driver()
    d.at(10, 0.0); d.at(10, 50.0); d.at(25, 50.0)
    d.hold(80, 0.0)
    key = detect.incident_key("ping.loss", "1.1.1.1")
    assert detect.state_of(key) == detect.COOLDOWN
    d.hold(400, 0.0)                  # past cooldown_s=300
    assert detect.state_of(key) == detect.OK


# ---------- baseline freezing ----------

def _center(sig="ping.rtt_med", ent="gw"):
    return baseline.get(sig, ent).center


def test_baseline_learns_only_in_ok():
    """Invariant 8. Freezing outside OK is what stops a six-hour outage teaching the node
    that the outage is normal."""
    d = Driver(signal="host.temp", entity="cpu")   # trip=75, clear=70, for_s=60
    for _ in range(40):
        d.at(10, 40.0)
    learned = _center("host.temp", "cpu")
    assert 39.0 < learned < 41.0

    d.at(10, 90.0)                    # ARMED
    d.hold(120, 90.0)                 # OPEN, sustained
    assert _center("host.temp", "cpu") == pytest.approx(learned), \
        "baseline moved while an incident was open"

    d.hold(200, 40.0)                 # CLOSING -> close -> COOLDOWN
    assert _center("host.temp", "cpu") == pytest.approx(learned), \
        "baseline moved during recovery/cooldown"


def test_mad_floor_stops_trivial_wobble_becoming_huge_z():
    """A near-constant signal drives dev toward zero; without a floor a 0.1 ms jitter reads
    as z in the hundreds and manufactures incidents."""
    b = baseline.Baseline(center=20.0, dev=0.0, n=100, updated=0.0)
    rule = detect.rule_for("ping.rtt_med")
    z = b.z(20.1, rule.abs_floor, rule.rel_floor)
    assert abs(z) < 1.0, f"z={z} -- MAD floor is not being applied"


# ---------- signal kinds gate the generic fallback ----------

@pytest.mark.parametrize("signal,kind", [
    ("net.err_rate", detect.COUNTER_RATE),
    ("some.binary", detect.BINARY),
    ("some.state", detect.STATE),
])
def test_z_fallback_never_applies_to_incompatible_kinds(signal, kind, monkeypatch):
    """z on a zero-inflated counter or a {0,1} state produces confident nonsense."""
    monkeypatch.setitem(detect.SIGNAL_KINDS, signal, kind)
    rule = detect.rule_for(signal)
    assert rule.trip_z is None and rule.clear_z is None


def test_z_fallback_applies_to_continuous_kinds():
    rule = detect.rule_for("something.unmapped")   # defaults to gauge
    assert rule.trip_z == 4.0


# ---------- expiry: absolute vs relative ----------

def test_expiry_of_absolute_rule_keeps_baseline_frozen(monkeypatch):
    """A disk at 96% is still bad after 24 hours. Thawing there would silently turn a
    permanent fault into the new normal -- expiry must not become an auto-acknowledge.

    The signal is still reporting (last_mono is fresh); only the incident's wall-clock age has
    passed max-open. A signal that had *stopped* reporting takes the 'stale' path instead,
    which is checked separately."""
    d = Driver(signal="disk.used_pct", entity="/")   # absolute=True, for_s=300
    for _ in range(40):
        d.at(30, 50.0)
    learned = _center("disk.used_pct", "/")
    d.hold(400, 96.0, step=30.0)                     # open
    monkeypatch.setattr(config, "INCIDENT_MAX_OPEN_S", 0.0)
    acts = detect.sweep(d.mono, d.wall)
    assert [a.op for a in acts] == ["persist"]
    assert _center("disk.used_pct", "/") == pytest.approx(learned), \
        "an absolute safety rule was trained away by expiry"


def test_expiry_of_relative_rule_thaws_baseline(monkeypatch):
    d = Driver(signal="ping.rtt_med", entity="gw")   # absolute=False
    for _ in range(60):
        d.at(10, 20.0)
    assert baseline.get("ping.rtt_med", "gw").n >= 30
    d.hold(120, 500.0)                               # open on the dynamic threshold
    monkeypatch.setattr(config, "INCIDENT_MAX_OPEN_S", 0.0)
    acts = detect.sweep(d.mono, d.wall)
    assert [a.op for a in acts] == ["expire"]
    assert baseline.get("ping.rtt_med", "gw").n == 0, "relative baseline should have thawed"


def test_stale_wins_over_expiry_when_the_signal_stopped():
    """If a probe died, that is the more useful explanation than 'ran too long'."""
    d = Driver(signal="disk.used_pct", entity="/")
    d.hold(400, 96.0, step=30.0)
    acts = detect.sweep(d.mono + config.SIGNAL_STALE_S + 1, d.wall + config.SIGNAL_STALE_S + 1)
    assert [a.op for a in acts] == ["stale"]


def test_stale_signal_closes_the_incident():
    """A dead probe must not leave an incident open forever; the hub cannot distinguish that
    from a genuine ongoing fault."""
    d = Driver()
    d.at(10, 0.0); d.at(10, 50.0); d.at(25, 50.0)
    acts = detect.sweep(d.mono + config.SIGNAL_STALE_S + 1, d.wall + config.SIGNAL_STALE_S + 1)
    assert [a.op for a in acts] == ["stale"]


# ---------- clock behaviour ----------

def test_wall_clock_stepping_backwards_does_not_fire_early():
    """Durations are monotonic. An NTP step at boot -- routine on a Pi -- must not be able to
    satisfy a debounce."""
    d = Driver()
    d.at(10, 0.0)
    d.at(10, 50.0)                    # ARMED
    d.wall -= 7200.0                  # clock steps two hours backwards
    d.mono += 5.0                     # only 5s of real time passed
    acts = detect.evaluate("ping.loss", "1.1.1.1", 50.0, d.wall, d.mono)
    assert acts == [], "a backwards wall-clock step satisfied the debounce"


def test_restore_from_negative_age_restarts_the_timer():
    key = detect.incident_key("ping.loss", "1.1.1.1")
    detect.restore(key, "ping.loss", "1.1.1.1", detect.OPEN,
                   changed_wall=2_000_000.0,      # in the future relative to now_wall
                   opened_wall=2_000_000.0, worst=50.0,
                   now_mono=100.0, now_wall=1_000_000.0)
    acts = detect.sweep(100.0 + config.SIGNAL_STALE_S - 1, 1_000_000.0)
    assert acts == [], "a future-dated state should not immediately trigger a sweep"


def test_closing_is_restored_as_open():
    """Fail toward a longer incident, never toward a close we never observed."""
    key = detect.incident_key("ping.loss", "gw")
    detect.restore(key, "ping.loss", "gw", detect.CLOSING, changed_wall=1_000.0,
                   opened_wall=900.0, worst=50.0, now_mono=10_000.0, now_wall=1_100.0)
    assert detect.state_of(key) == detect.OPEN


# ---------- memory bound ----------

def test_registry_is_hard_bounded_by_signal_max():
    """A node churning container names or interface aliases must not be able to grow the
    registry. The cap is enforced in feed(), not trusted upstream."""
    for i in range(500):
        signals.feed("host.mem", f"entity-{i}", 10.0)
    n, nbytes = signals.stats()
    assert n == config.SIGNAL_MAX
    assert nbytes < 100_000, f"{nbytes} bytes exceeds the stated ~85 KB ceiling"
    assert signals.drops() > 0


def test_drop_warning_is_rate_limited():
    for i in range(config.SIGNAL_MAX + 5):
        signals.feed("host.mem", f"e{i}", 1.0)
    assert signals.should_warn_drops(1000.0) is True
    assert signals.should_warn_drops(1100.0) is False, "unthrottled warnings become the flood"
    assert signals.should_warn_drops(1000.0 + 3601) is True


def test_ring_tail_returns_oldest_first_and_is_bounded():
    for i in range(200):
        signals.feed("host.temp", "cpu", float(i))
    r = signals.ring("host.temp", "cpu")
    assert len(r) == config.SIGNAL_RING
    tail = r.tail(5)
    assert [v for _t, v in tail] == [195.0, 196.0, 197.0, 198.0, 199.0]


# ---------- rule overrides ----------

def test_sparse_overrides_replace_only_named_fields():
    detect.reload_rules("ping.loss:trip=15,for_s=30")
    r = detect.rule_for("ping.loss")
    assert r.trip == 15.0 and r.for_s == 30.0
    assert r.clear == 1.0 and r.severity == "error", "unnamed fields must survive an override"


def test_malformed_override_is_skipped_not_raised():
    """A typo in an env var must not stop a node from monitoring."""
    detect.reload_rules("ping.loss:trip=notanumber;garbage;host.temp:trip=70")
    assert detect.rule_for("ping.loss").trip == 10.0
    assert detect.rule_for("host.temp").trip == 70.0


def test_rule_hash_changes_with_the_rule():
    a = detect.rule_hash(detect.rule_for("ping.loss"))
    detect.reload_rules("ping.loss:trip=15")
    b = detect.rule_hash(detect.rule_for("ping.loss"))
    assert a != b, "incidents would be uninterpretable after a rule change"


# ---------- rule table hygiene ----------

def test_every_known_signal_has_an_explicit_rule():
    """The fallback exists for signals we have not thought about yet. Any signal we HAVE
    named must carry a deliberate rule -- inheriting the fallback silently gives it
    direction='+', which is wrong for every lower-is-worse signal."""
    missing = sorted(set(detect.SIGNAL_KINDS) - set(detect.RULES))
    assert missing == [], f"named signals falling through to the fallback: {missing}"


def test_lower_is_worse_signals_declare_their_direction():
    """RSSI in dBm is the trap: -50 is good, -90 is bad. A '+' rule there opens an incident
    when reception improves."""
    r = detect.rule_for("wifi.rssi")
    assert r.direction == "-" and r.peak_mode == "min"
    assert r.trip is not None and r.clear is not None and r.trip < r.clear, \
        "for a lower-is-worse rule the trip threshold must sit BELOW the clear threshold"


def test_wifi_rssi_trips_on_a_weak_signal_not_a_strong_one():
    d = Driver(signal="wifi.rssi", entity="wlan0")
    d.hold(300, -85.0, step=30.0)                 # weak: should open
    assert d.ops().count("open") == 1
    detect.reset(); signals.reset()
    d2 = Driver(signal="wifi.rssi", entity="wlan0")
    d2.hold(300, -40.0, step=30.0)                # strong: must NOT open
    assert d2.ops() == []


def test_hysteresis_bands_are_ordered_correctly_for_every_rule():
    """trip and clear must bracket a real band, in the direction the rule declares. A rule
    with them the wrong way round would trip and clear on the same sample and flap forever."""
    for name, r in detect.RULES.items():
        if r.trip is None or r.clear is None:
            continue
        if r.direction == "+":
            assert r.trip > r.clear, f"{name}: trip must exceed clear for a higher-is-worse rule"
        else:
            assert r.trip < r.clear, f"{name}: trip must be below clear for a lower-is-worse rule"


def test_counter_and_capacity_rules_do_not_carry_z():
    """z on a zero-inflated counter rate or against a capacity ceiling is confident nonsense."""
    for name, r in detect.RULES.items():
        if r.kind in (detect.COUNTER_RATE, detect.CAPACITY):
            assert r.trip_z is None, f"{name} ({r.kind}) should not use a z threshold"
            assert r.trip is not None, f"{name} ({r.kind}) needs an absolute threshold"
