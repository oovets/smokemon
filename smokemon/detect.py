"""Online anomaly rules and the incident state machine.

This is the policy core: it decides what counts as an anomaly, how long it must persist, how
far it must recover before we believe it, and how long to stay quiet afterwards. It is also
the module most likely to accumulate complexity over time, so two boundaries are enforced
here and must stay enforced:

  * It never touches SQLite. `evaluate()` returns a list of Actions describing what should be
    persisted; `incidents.py` decides how and owns the transaction. The whole state machine is
    therefore testable against synthetic sample sequences with no database at all.
  * It never decides incident identity. Whether an `open` continues an existing uid or starts
    a new one is the reopen policy, which lives in `incidents.py` next to the persisted
    `closed_wall` it needs.

All durations are measured on time.monotonic(). Wall clock is carried alongside purely so the
stored rows have a timestamp a human can read. Pi and Jetson nodes NTP-step at boot, routinely
by hours; a debounce measured on wall clock would either fire on the first sample or never.
"""

from __future__ import annotations

import hashlib
import math
from typing import NamedTuple

from . import baseline, config, signals

DETECTOR_VERSION = 1

# ---------- signal kinds ----------
# The kind decides whether a robust-z rule is meaningful at all. Applying z to a counter rate
# (zero-inflated, bursty) or a binary state ({0,1} has no useful spread) produces confident
# nonsense, so the generic fallback rule is restricted to the continuous kinds.
GAUGE = "gauge"
LATENCY = "latency"
RATIO = "ratio"
CAPACITY = "capacity"       # absolute thresholds are the meaningful ones; z adds nothing
COUNTER_RATE = "counter_rate"
BINARY = "binary"
STATE = "state"

Z_ELIGIBLE = frozenset({GAUGE, LATENCY, RATIO})

SIGNAL_KINDS: dict[str, str] = {
    "ping.loss": RATIO,
    "ping.loss_run": RATIO,
    "ping.rtt_med": LATENCY,
    "host.temp": GAUGE,
    "host.mem": GAUGE,
    "host.swap": GAUGE,
    "host.psi_cpu": GAUGE,
    "host.psi_io": GAUGE,
    "disk.used_pct": CAPACITY,
    "disk.inode_used_pct": CAPACITY,
    "net.err_rate": COUNTER_RATE,
    "wifi.rssi": GAUGE,
}


class Rule(NamedTuple):
    signal: str
    kind: str
    absolute: bool          # a safety threshold that must never be trained away by expiry
    direction: str          # "+" high is bad, "-" low is bad
    trip: float | None      # absolute trip threshold (None -> z-only)
    clear: float | None     # absolute clear threshold; strictly inside `trip`
    trip_z: float | None
    clear_z: float | None
    for_s: float            # breach must hold this long before an incident opens (debounce)
    clear_for_s: float      # recovery must hold this long before it closes (hysteresis hold)
    # How long after a close the incident stays "recent": the baseline remains frozen, and a
    # re-trip inside this window skips ARMED entirely and reopens immediately. NOT a quiet
    # period -- the condition already proved it persists once, so making it re-earn the
    # debounce would just delay a recurrence we already believe. Whether that reopen continues
    # the same incident is a separate question, answered by INCIDENT_REOPEN_WINDOW_S.
    cooldown_s: float
    severity: str
    peak_mode: str          # "max" | "min" | "max_abs_z" -- what "worst" means for this signal
    abs_floor: float = 0.0  # z denominator floor, absolute
    rel_floor: float = 0.0  # z denominator floor, relative to |centre|
    min_baseline_n: int = 30
    dynamic: str = ""       # "" | "rtt" -- named dynamic threshold formula, see _thresholds


def _r(signal, **kw) -> Rule:
    base = dict(kind=SIGNAL_KINDS.get(signal, GAUGE), absolute=False, direction="+",
                trip=None, clear=None, trip_z=None, clear_z=None, for_s=60.0,
                clear_for_s=180.0, cooldown_s=600.0, severity="warn", peak_mode="max")
    base.update(kw)
    return Rule(signal=signal, **base)


# The hysteresis band is the gap between `trip` and `clear`; the debounce is the pair of hold
# times. analyze.py approximated both with LOSS_FLOOR_PCT / LOSS_INCIDENT_PEAK / MIN_RUN_CYCLES
# -- the floor was really a clear threshold and the peak really a trip threshold, and the run
# count was a debounce expressed in samples. Expressing the hold in seconds is the point of the
# pivot: the same rule now behaves identically whatever rate the probe happens to sample at.
RULES: dict[str, Rule] = {
    "ping.loss": _r("ping.loss", absolute=True, trip=10.0, clear=1.0,
                    for_s=20.0, clear_for_s=60.0, cooldown_s=300.0, severity="error"),
    "ping.loss_run": _r("ping.loss_run", absolute=True, trip=99.5, clear=50.0,
                        for_s=20.0, clear_for_s=60.0, cooldown_s=300.0, severity="crit"),
    "ping.rtt_med": _r("ping.rtt_med", dynamic="rtt", trip_z=4.0, clear_z=2.0,
                       for_s=20.0, clear_for_s=90.0, cooldown_s=300.0,
                       abs_floor=2.0, rel_floor=0.05),
    "host.temp": _r("host.temp", absolute=True,
                    trip=config.THROTTLE_TEMP_C - 5.0, clear=config.THROTTLE_TEMP_C - 10.0,
                    for_s=60.0, clear_for_s=180.0, cooldown_s=600.0),
    "host.mem": _r("host.mem", absolute=True, trip=92.0, clear=80.0,
                   for_s=120.0, clear_for_s=300.0, cooldown_s=900.0),
    "host.swap": _r("host.swap", absolute=True, trip=25.0, clear=10.0,
                    for_s=300.0, clear_for_s=600.0, cooldown_s=1800.0),
    "disk.used_pct": _r("disk.used_pct", absolute=True, trip=92.0, clear=85.0,
                        for_s=300.0, clear_for_s=900.0, cooldown_s=3600.0, severity="error"),
    "disk.inode_used_pct": _r("disk.inode_used_pct", absolute=True, trip=90.0, clear=80.0,
                              for_s=300.0, clear_for_s=900.0, cooldown_s=3600.0,
                              severity="error"),
    "host.psi_cpu": _r("host.psi_cpu", absolute=True, trip=50.0, clear=20.0,
                       for_s=120.0, clear_for_s=300.0, cooldown_s=900.0),
    "host.psi_io": _r("host.psi_io", absolute=True, trip=40.0, clear=15.0,
                      for_s=120.0, clear_for_s=300.0, cooldown_s=900.0),
    "net.err_rate": _r("net.err_rate", absolute=True, trip=1.0, clear=0.1,
                       for_s=60.0, clear_for_s=180.0, cooldown_s=600.0),
    # RSSI is dBm: -50 is a good signal, -90 is a bad one. Without an explicit direction this
    # inherits the fallback's "+" and opens an incident every time reception IMPROVES.
    "wifi.rssi": _r("wifi.rssi", direction="-", trip=-80.0, clear=-75.0, peak_mode="min",
                    for_s=120.0, clear_for_s=300.0, cooldown_s=900.0),
}

FALLBACK = _r("*", trip_z=4.0, clear_z=3.0, for_s=60.0, clear_for_s=180.0, cooldown_s=600.0)


# ---------- rule overrides ----------

_NUMERIC = {"trip", "clear", "trip_z", "clear_z", "for_s", "clear_for_s", "cooldown_s",
            "abs_floor", "rel_floor", "min_baseline_n"}


def parse_overrides(spec: str) -> dict[str, dict]:
    """'ping.loss:trip=15,for_s=30;host.temp:trip=75' -> {signal: {field: value}}.

    Sparse per-field, so an operator never has to restate a whole rule to move one number.
    A malformed clause is skipped rather than raised: a typo in an env var must not stop a
    node from monitoring."""
    out: dict[str, dict] = {}
    for clause in spec.split(";"):
        clause = clause.strip()
        if not clause or ":" not in clause:
            continue
        sig, _, fields = clause.partition(":")
        sig = sig.strip()
        got: dict = {}
        for pair in fields.split(","):
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if not k or not v:
                continue
            if k in _NUMERIC:
                try:
                    got[k] = int(v) if k == "min_baseline_n" else float(v)
                except ValueError:
                    continue
            elif k in ("severity", "direction", "peak_mode"):
                got[k] = v
        if sig and got:
            out[sig] = got
    return out


def _apply_overrides(rules: dict[str, Rule], overrides: dict[str, dict]) -> dict[str, Rule]:
    out = dict(rules)
    for sig, fields in overrides.items():
        base = out.get(sig)
        if base is None:
            base = _r(sig)
        out[sig] = base._replace(**fields)
    return out


def rule_hash(rule: Rule) -> str:
    """Identity of the effective rule, after overrides. An incident from last month is not
    interpretable after a threshold change unless the row says which thresholds it was
    evaluated under."""
    body = "|".join(f"{f}={getattr(rule, f)!r}" for f in rule._fields)
    return hashlib.sha1(body.encode()).hexdigest()[:12]


_rules: dict[str, Rule] = _apply_overrides(RULES, parse_overrides(config.RULES_SPEC))


def rule_for(signal: str) -> Rule:
    r = _rules.get(signal)
    if r is not None:
        return r
    kind = SIGNAL_KINDS.get(signal, GAUGE)
    if kind not in Z_ELIGIBLE:
        return FALLBACK._replace(signal=signal, kind=kind, trip_z=None, clear_z=None)
    return FALLBACK._replace(signal=signal, kind=kind)


def reload_rules(spec: str | None = None) -> None:
    global _rules
    _rules = _apply_overrides(RULES, parse_overrides(
        config.RULES_SPEC if spec is None else spec))


# ---------- state machine ----------

OK, ARMED, OPEN, CLOSING, COOLDOWN = "ok", "armed", "open", "closing", "cooldown"


class Action(NamedTuple):
    """A declarative instruction for incidents.py. detect never persists anything itself."""
    op: str               # open | close | expire | persist | stale | sample
    key: str
    signal: str
    entity: str
    rule: Rule
    wall: float
    value: float | None = None
    threshold: float | None = None
    z: float | None = None
    center: float | None = None
    mad: float | None = None
    pre: tuple = ()       # [(wall, value)] baseline before the breach began, open only
    onset: tuple = ()     # [(wall, value)] the breach coming on, open only
    phase: str = ""       # sample only: during | post
    # Running extremum over EVERY sample seen, including ones decimation discarded -- the
    # worst moment of an incident is frequently in a sample that was not kept.
    worst: float | None = None
    detail: str = ""


class _State:
    __slots__ = ("state", "signal", "entity", "rule", "since_mono", "onset_mono",
                 "changed_wall", "opened_wall", "last_mono", "worst", "worst_z")

    def __init__(self, signal: str, entity: str, rule: Rule) -> None:
        self.state = OK
        self.signal = signal
        self.entity = entity
        self.rule = rule
        self.since_mono = 0.0     # when the current candidate/hold started
        self.onset_mono = 0.0     # when the breach first appeared (start of ARMED)
        self.changed_wall = 0.0
        self.opened_wall = 0.0
        self.last_mono = 0.0
        self.worst = None
        self.worst_z = 0.0


_states: dict[str, _State] = {}


def incident_key(signal: str, entity: str) -> str:
    """Identity of the CONDITION -- stable forever, derived only from configuration. The
    identity of one OCCURRENCE is the uid, minted by incidents.py."""
    return f"{config.NODE}/{signal}/{entity}"


def _breach(value: float, rule: Rule, thresh: float | None, z: float | None) -> bool:
    """Trip test. Absolute OR z, never AND: z is the addition that gives per-node context, not
    a second hurdle. A rule with one side None degenerates cleanly to the other."""
    if thresh is not None:
        if (value > thresh) if rule.direction == "+" else (value < thresh):
            return True
    if z is not None and rule.trip_z is not None:
        return (z > rule.trip_z) if rule.direction == "+" else (z < -rule.trip_z)
    return False


def _clearing(value: float, rule: Rule, clear: float | None, z: float | None) -> bool:
    """Recovery test: must be clear on BOTH axes (the negation of an OR)."""
    if clear is not None:
        still_bad = (value > clear) if rule.direction == "+" else (value < clear)
        if still_bad:
            return False
    if z is not None and rule.clear_z is not None:
        still_bad = (z > rule.clear_z) if rule.direction == "+" else (z < -rule.clear_z)
        if still_bad:
            return False
    return True


def _thresholds(rule: Rule, b: baseline.Baseline) -> tuple[float | None, float | None]:
    """Effective (trip, clear). `dynamic="rtt"` reproduces analyze._detect_latency's
    max(base*3, base+30) but sourced from the persisted per-node baseline rather than the
    median of the visible window -- a streaming detector has no window, and the window median
    was itself poisonable by a long incident sitting inside it."""
    if rule.dynamic == "rtt":
        if not b.ready(rule.min_baseline_n) or b.center <= 0.0:
            return (None, None)
        return (max(b.center * 3.0, b.center + 30.0), b.center * 1.5)
    return (rule.trip, rule.clear)


def evaluate(signal: str, entity: str = "", value: float | None = None,
             wall: float | None = None, mono: float | None = None) -> list[Action]:
    """Feed one sample and advance that signal's state machine. Returns actions to persist.

    Never raises on ordinary input: a probe handing over a None or a NaN is a gap, not an
    anomaly, and must not take down the collector."""
    fed = signals.feed(signal, entity, value, wall, mono)
    if fed is None or value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    wall, mono = fed
    rule = rule_for(signal)
    key = incident_key(signal, entity)
    st = _states.get(key)
    if st is None:
        st = _states[key] = _State(signal, entity, rule)
    st.rule = rule
    st.last_mono = mono

    b = baseline.get(signal, entity)
    z = None
    if rule.kind in Z_ELIGIBLE and (rule.trip_z is not None or rule.dynamic):
        if b.ready(rule.min_baseline_n):
            z = b.z(value, rule.abs_floor, rule.rel_floor)
    trip_t, clear_t = _thresholds(rule, b)

    breaching = _breach(value, rule, trip_t, z)
    clearing = _clearing(value, rule, clear_t, z)
    acts: list[Action] = []

    if st.state == OK:
        if breaching:
            st.state, st.since_mono, st.onset_mono = ARMED, mono, mono
        else:
            # The ONE place the baseline learns. Freezing everywhere else is what stops a
            # six-hour outage teaching the node that 100% loss is normal here, and why
            # COOLDOWN freezes too -- the recovery tail is not representative either.
            baseline.update(signal, entity, value, wall,
                            abs_floor=rule.abs_floor, rel_floor=rule.rel_floor)
        return acts

    if st.state == ARMED:
        if not breaching:
            st.state = OK          # NOTHING is written. This is the flap filter.
            return acts
        if mono - st.since_mono >= rule.for_s:
            st.state, st.changed_wall, st.opened_wall = OPEN, wall, wall
            st.worst, st.worst_z = value, (z or 0.0)
            acts.append(_open_action(st, key, rule, wall, value, trip_t, z, b))
        return acts

    if st.state in (OPEN, CLOSING):
        _track_worst(st, value, z)
        if breaching:
            if st.state == CLOSING:
                st.state = OPEN     # flap absorber: a bouncing signal is ONE incident
            acts.append(Action(op="sample", key=key, signal=signal, entity=entity, rule=rule,
                               wall=wall, value=value, phase="during", worst=st.worst))
        elif st.state == OPEN:
            st.state, st.since_mono = CLOSING, mono
            acts.append(Action(op="sample", key=key, signal=signal, entity=entity, rule=rule,
                               wall=wall, value=value, phase="during", worst=st.worst))
        elif clearing and mono - st.since_mono >= rule.clear_for_s:
            st.state, st.changed_wall, st.since_mono = COOLDOWN, wall, mono
            acts.append(Action(
                op="close", key=key, signal=signal, entity=entity, rule=rule, wall=wall,
                value=value, threshold=clear_t, z=z, worst=st.worst,
                detail=f"recovered at {_fmt(value)}"))
        elif not clearing:
            st.since_mono = mono    # inside the hysteresis band: neither bad nor believed well
        return acts

    if st.state == COOLDOWN:
        if breaching:
            # No ARMED phase here: the condition already proved it persists, so a re-trip
            # inside cooldown is believed immediately. Onset is therefore this sample.
            st.state, st.changed_wall, st.opened_wall = OPEN, wall, wall
            st.onset_mono = mono
            st.worst, st.worst_z = value, (z or 0.0)
            # incidents.py decides whether this continues the previous uid (within
            # reopen_window_s) or mints a new one -- it holds the persisted closed_wall.
            acts.append(_open_action(st, key, rule, wall, value, trip_t, z, b))
        elif mono - st.since_mono >= rule.cooldown_s:
            st.state = OK
        return acts

    return acts


def _open_action(st: _State, key: str, rule: Rule, wall: float, value: float,
                 trip_t: float | None, z: float | None, b: baseline.Baseline) -> Action:
    """Build the open action, splitting the ring at the moment the breach began.

    Everything before onset is baseline; everything from onset to confirmation is the anomaly
    coming on. Labelling the latter 'pre' would put the anomaly inside its own reference
    window, which is both wrong to read and wrong to plot."""
    ring = signals.ring(st.signal, st.entity)
    pre = ring.before(st.onset_mono, config.INCIDENT_PRE_SAMPLES) if ring else []
    onset = ring.since(st.onset_mono, config.INCIDENT_DURING_HEAD) if ring else []
    return Action(
        op="open", key=key, signal=st.signal, entity=st.entity, rule=rule, wall=wall,
        value=value, threshold=trip_t, z=z, center=b.center,
        mad=b.scale(rule.abs_floor, rule.rel_floor),
        pre=tuple(pre), onset=tuple(onset),
        detail=_detail(st.signal, st.entity, value, trip_t, z))


def _track_worst(st: _State, value: float, z: float | None) -> None:
    """"Worst" is not always "max": free memory is worst at its minimum, latency at its
    maximum. The rule declares which, and the stored row carries the mode so a reader never
    has to guess."""
    mode = st.rule.peak_mode
    if st.worst is None:
        st.worst = value
    elif mode == "min":
        st.worst = min(st.worst, value)
    elif mode == "max_abs_z":
        if abs(z or 0.0) > abs(st.worst_z):
            st.worst, st.worst_z = value, (z or 0.0)
    else:
        st.worst = max(st.worst, value)


def sweep(now_mono: float, now_wall: float) -> list[Action]:
    """Age-based transitions for signals that stopped reporting, and for incidents that have
    outlived INCIDENT_MAX_OPEN_S.

    Without the stale path a probe that dies leaves its incident open forever, and the hub
    cannot tell that from a genuine ongoing fault."""
    acts: list[Action] = []
    for key, st in list(_states.items()):
        if st.state not in (OPEN, CLOSING):
            continue
        rule = st.rule
        if now_mono - st.last_mono >= config.SIGNAL_STALE_S:
            st.state, st.changed_wall, st.since_mono = COOLDOWN, now_wall, now_mono
            acts.append(Action(op="stale", key=key, signal=st.signal, entity=st.entity,
                               rule=rule, wall=now_wall, worst=st.worst,
                               detail="signal stopped reporting"))
            continue
        if now_wall - st.opened_wall >= config.INCIDENT_MAX_OPEN_S:
            st.state, st.changed_wall, st.since_mono = COOLDOWN, now_wall, now_mono
            if rule.absolute:
                # An absolute safety rule must NOT be trained away. Disk at 96% is still bad
                # after 24 hours. Close the run so it does not sit open forever, but keep the
                # baseline frozen and keep saying so, rather than letting expiry become a
                # silent auto-acknowledge of a permanent fault.
                acts.append(Action(op="persist", key=key, signal=st.signal, entity=st.entity,
                                   rule=rule, wall=now_wall, value=st.worst, worst=st.worst,
                                   detail="condition persists past max-open"))
            else:
                acts.append(Action(op="expire", key=key, signal=st.signal, entity=st.entity,
                                   rule=rule, wall=now_wall, value=st.worst, worst=st.worst,
                                   detail="expired; relearning baseline"))
                baseline.thaw(st.signal, st.entity)
    return acts


def _fmt(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _detail(signal: str, entity: str, value: float, thresh: float | None, z: float | None) -> str:
    where = f" on {entity}" if entity else ""
    if thresh is not None:
        return f"{signal}{where} at {_fmt(value)} (threshold {_fmt(thresh)})"
    if z is not None:
        return f"{signal}{where} at {_fmt(value)} (z={z:.1f} vs node baseline)"
    return f"{signal}{where} at {_fmt(value)}"


def state_of(key: str) -> str:
    st = _states.get(key)
    return st.state if st else OK


def restore(key: str, signal: str, entity: str, state: str, changed_wall: float,
            opened_wall: float, worst: float | None, now_mono: float, now_wall: float) -> None:
    """Rehydrate a state machine from persisted incident_state after a restart.

    ARMED is never persisted and so is never restored: an unconfirmed anomaly does not survive
    a restart, and its debounce starts over. That is the intended cost of refusing to write
    candidates to disk.

    CLOSING is restored as OPEN with the hold timer reset -- failing toward a longer incident,
    never toward a close we did not actually observe.

    Monotonic timers have no meaning across processes, so each is re-seeded from the persisted
    wall clock. If that arithmetic comes out negative (the clock stepped backwards while we
    were down) the timer is treated as freshly started: fail toward a longer debounce, never
    toward a spurious incident."""
    st = _states.get(key)
    if st is None:
        st = _states[key] = _State(signal, entity, rule_for(signal))
    st.state = OPEN if state == CLOSING else state
    st.changed_wall = changed_wall
    st.opened_wall = opened_wall or changed_wall
    st.worst = worst
    age = now_wall - changed_wall
    st.since_mono = now_mono if age < 0 else now_mono - age
    st.last_mono = now_mono


def reset() -> None:
    """Tests only."""
    _states.clear()
