"""Incident persistence: uid identity, append-only transitions, and the transaction boundary.

`incidents` is a transition LOG, not a mutable row. That is forced by the shipper: gather()
walks each table with a strict monotonic rowid cursor and the hub inserts with INSERT OR
IGNORE. An UPDATE to an already-shipped row changes no rowid, so the hub would never see it --
the incident would close on the node and stay open forever on the hub. Expressing the
lifecycle as separate rows sharing a `uid` also buys replay, idempotence, readable history and
correct handling of late arrival, so the constraint and the right design agree.

Child rows key on the node-generated `uid`, never on a local rowid. The hub only remaps ids
for one legacy table; a rowid foreign key would be meaningless there and would recreate the
ping_rtts bug where redelivered children are silently dropped. With a uid, a sample that
arrives before its parent is an unjoined-but-valid row that completes when the parent lands.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from . import config, core, detect, events, schema

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS incident_state (
  key         TEXT PRIMARY KEY,
  uid         TEXT NOT NULL,
  signal      TEXT NOT NULL,
  entity      TEXT NOT NULL DEFAULT '',
  rule_hash   TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL,
  opened_ts   REAL,
  changed_ts  REAL,
  closed_wall REAL,
  last_ts     REAL,
  worst_value REAL,
  n_written   INTEGER NOT NULL DEFAULT 0,
  next_step_s REAL NOT NULL DEFAULT 0,
  last_kept   REAL);
"""


def ensure_table(conn) -> None:
    """Node-local. Deliberately NOT in schema._BODY -- membership there is the ship switch,
    and this is mutable working state the hub must never receive (log_cursors precedent)."""
    conn.executescript(_DDL)


def mint_uid(key: str, opened_wall: float) -> str:
    """Identity of one OCCURRENCE. The node name is inside `key`, so this is globally unique
    without the hub needing to compound it."""
    return hashlib.sha1(f"{key}|{opened_wall:.3f}".encode()).hexdigest()[:16]


def _row(conn, key: str):
    return conn.execute(
        "SELECT uid, signal, entity, state, opened_ts, changed_ts, closed_wall, worst_value, "
        "n_written, next_step_s, last_kept FROM incident_state WHERE key=?", (key,)).fetchone()


def _upsert(conn, key: str, **cols) -> None:
    """Write a complete state row. Used when an incident opens, which is the only moment we
    hold values for every NOT NULL column."""
    names = ",".join(cols)
    place = ",".join("?" * len(cols))
    sets = ",".join(f"{c}=excluded.{c}" for c in cols)
    conn.execute(f"INSERT INTO incident_state (key,{names}) VALUES (?,{place}) "
                 f"ON CONFLICT(key) DO UPDATE SET {sets}",
                 (key, *cols.values()))


def _update(conn, key: str, **cols) -> None:
    """Patch selected columns of an existing state row.

    Deliberately not an upsert: an INSERT ... ON CONFLICT still has to satisfy NOT NULL on the
    insert path even when the row already exists, so a partial upsert fails on uid/signal/state.
    A missing row here means the incident was never opened, and silently inserting a half-built
    one would be worse than doing nothing."""
    sets = ",".join(f"{c}=?" for c in cols)
    conn.execute(f"UPDATE incident_state SET {sets} WHERE key=?", (*cols.values(), key))


def open_count(conn) -> int:
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM incident_state WHERE state IN ('open','closing')").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def active_uid(conn) -> str | None:
    """Newest currently-open uid, for stamping evidence. None when nothing is open -- a
    governor shed still captures a log excerpt, just unlinked."""
    try:
        row = conn.execute(
            "SELECT uid FROM incident_state WHERE state IN ('open','closing') "
            "ORDER BY changed_ts DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def load_open(conn, now_mono: float | None = None, now_wall: float | None = None) -> int:
    """Rehydrate detect's state machines from disk. Returns how many were restored.

    This is why incident_state is a table and not an in-memory set: a still-broken condition
    must RESUME its incident rather than open a second one, so the hub sees one incident
    spanning the restart. events.py's in-memory _active set cannot do that, which is exactly
    why detect.py is a separate path rather than an extension of it."""
    ensure_table(conn)
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    try:
        rows = conn.execute(
            "SELECT key, signal, entity, state, opened_ts, changed_ts, worst_value "
            "FROM incident_state WHERE state IN ('open','closing','cooldown')").fetchall()
    except sqlite3.OperationalError:
        return 0
    for key, signal, entity, state, opened, changed, worst in rows:
        detect.restore(key, signal, entity or "", state, changed or now_wall,
                       opened or changed or now_wall, worst, now_mono, now_wall)
    return len(rows)


# ---------- decimation ----------

def _keep_during(n_written: int, wall: float, last_kept: float | None,
                 next_step: float) -> tuple[bool, float]:
    """Should this DURING sample be persisted, and what is the next step size?

    Head samples go in at native cadence so the shape right after the trigger is intact; after
    that an exponential ladder keeps coverage of a long incident without letting row count
    grow with duration. A three-day outage and a three-minute one cost nearly the same."""
    if n_written >= config.INCIDENT_DURING_MAX:
        return (False, next_step)
    if n_written < config.INCIDENT_DURING_HEAD:
        return (True, config.INCIDENT_DURING_STEP0)
    if last_kept is None or wall - last_kept >= next_step:
        return (True, min(next_step * config.INCIDENT_DURING_GROWTH,
                          config.INCIDENT_DURING_STEP_MAX))
    return (False, next_step)


# ---------- applying detector actions ----------

def _transition(conn, *, uid, transition, act, worst=None, opened_ts=None,
                duration_s=None, n_samples=None) -> None:
    rule = act.rule
    schema.insert(conn, "incidents", [{
        "ts": act.wall, "uid": uid, "transition": transition,
        "signal": act.signal, "entity": act.entity, "kind": rule.kind,
        "rule": rule.signal, "rule_hash": detect.rule_hash(rule),
        "detector_version": detect.DETECTOR_VERSION, "schema_version": SCHEMA_VERSION,
        "severity": rule.severity if transition in ("open", "reopen", "persist") else "info",
        "value": act.value, "threshold": act.threshold, "baseline": act.center,
        "baseline_mad": act.mad, "z": act.z,
        "peak_mode": rule.peak_mode, "worst_value": worst,
        "comparison_direction": rule.direction,
        "opened_ts": opened_ts, "duration_s": duration_s, "n_samples": n_samples,
        "detail": act.detail,
    }])


def _samples(conn, uid: str, act, pairs, phase: str) -> None:
    if not pairs:
        return
    schema.insert(conn, "incident_samples", [
        {"ts": ts, "uid": uid, "phase": phase, "signal": act.signal,
         "entity": act.entity, "value": val} for ts, val in pairs])


def _apply_open(conn, act) -> None:
    prev = _row(conn, act.key)
    reopen = False
    if prev is not None:
        prev_uid, _s, _e, _st, prev_opened, _ch, closed_wall, _w, _n, _ns, _lk = prev
        # Reopen policy: the same uid continues the same OCCURRENCE only inside
        # reopen_window_s. Reusing it forever would fold a morning outage and an evening
        # outage into one semantically enormous incident; minting a new uid every time would
        # turn a flapping link into a dozen incidents. The window separates the two cases,
        # and is deliberately NOT the same knob as cooldown_s -- cooldown governs when the
        # detector may trip again, this governs whether it counts as the same event.
        if closed_wall and (act.wall - closed_wall) <= config.INCIDENT_REOPEN_WINDOW_S:
            uid, opened_ts, reopen = prev_uid, (prev_opened or act.wall), True
    if not reopen:
        uid, opened_ts = mint_uid(act.key, act.wall), act.wall

    # Under a storm, keep detecting but stop paying for evidence. Degrade detail, never
    # detection.
    budget = open_count(conn) < config.INCIDENT_MAX_OPEN
    _transition(conn, uid=uid, transition="reopen" if reopen else "open", act=act,
                worst=act.value, opened_ts=opened_ts)
    onset = act.onset or ((act.wall, act.value),)
    if budget:
        _samples(conn, uid, act, act.pre, "pre")
        _samples(conn, uid, act, onset, "during")
    _upsert(conn, act.key, uid=uid, signal=act.signal, entity=act.entity,
            rule_hash=detect.rule_hash(act.rule), state=detect.OPEN,
            opened_ts=opened_ts, changed_ts=act.wall, closed_wall=None, last_ts=act.wall,
            worst_value=act.value, n_written=len(onset) if budget else 0,
            next_step_s=config.INCIDENT_DURING_STEP0,
            last_kept=act.wall if budget else None)
    # Emit through the existing event path rather than calling ship.expedite() directly: that
    # keeps one coalesced shipping mechanism and reuses the feedback-loop guard. source is the
    # signal family, never "collector", which expedite deliberately skips.
    events._emit(conn, act.signal.split(".")[0], act.rule.severity,
                 "incident-open" if not reopen else "incident-reopen", act.detail)


def _apply_sample(conn, act) -> None:
    prev = _row(conn, act.key)
    if prev is None:
        return
    uid, _s, _e, _st, _op, _ch, _cw, worst, n_written, next_step, last_kept = prev
    keep, next_step2 = _keep_during(n_written, act.wall, last_kept, next_step)
    if keep:
        _samples(conn, uid, act, [(act.wall, act.value)], "during")
    # worst_value is persisted on every sample, kept or not: decimation routinely discards the
    # very sample that was the peak, and it must still survive a restart.
    _update(conn, act.key, last_ts=act.wall,
            worst_value=act.worst if act.worst is not None else worst,
            n_written=n_written + (1 if keep else 0), next_step_s=next_step2,
            last_kept=act.wall if keep else last_kept)


def _apply_terminal(conn, act, transition: str) -> None:
    prev = _row(conn, act.key)
    if prev is None:
        return
    uid, _s, _e, _st, opened_ts, _ch, _cw, worst, n_written, _ns, _lk = prev
    if act.worst is not None:
        worst = act.worst
    duration = (act.wall - opened_ts) if opened_ts else None
    _transition(conn, uid=uid, transition=transition, act=act, worst=worst,
                opened_ts=opened_ts, duration_s=duration, n_samples=n_written)
    from . import signals
    ring = signals.ring(act.signal, act.entity)
    if ring and transition == "close":
        _samples(conn, uid, act, ring.tail(config.INCIDENT_POST_SAMPLES), "post")
    _update(conn, act.key, state=detect.COOLDOWN, changed_ts=act.wall,
            closed_wall=act.wall, worst_value=worst)
    events._emit(conn, act.signal.split(".")[0], "info", f"incident-{transition}",
                 act.detail or "")


_HANDLERS = {
    "open": _apply_open,
    "sample": _apply_sample,
}
_TERMINAL = {"close": "close", "stale": "stale", "expire": "expired", "persist": "persistent"}


def apply(conn, actions) -> int:
    """Persist a batch of detector actions. Returns actions applied.

    Every action's transition row, its sample rows and its incident_state upsert land in ONE
    commit, so the node's own database is never internally inconsistent -- there is no window
    in which a close row exists without the state having moved, or vice versa. Batching also
    keeps an incident to a handful of commits rather than one per sample.

    Best-effort against a contended DB, matching the probe convention: a lost write means the
    condition is re-observed and re-acted on next cycle, which is far better than taking the
    collector down."""
    if not actions:
        return 0
    if config.DETECT_DRYRUN:
        for act in actions:
            if act.op != "sample":     # sample spam would drown the interesting transitions
                core.log(f"dryrun: {act.op} {act.signal}"
                         f"{'/' + act.entity if act.entity else ''} -- {act.detail}")
        return 0
    ensure_table(conn)
    n = 0
    try:
        for act in actions:
            handler = _HANDLERS.get(act.op)
            if handler is not None:
                handler(conn, act)
            elif act.op in _TERMINAL:
                _apply_terminal(conn, act, _TERMINAL[act.op])
            else:
                continue
            n += 1
        conn.commit()
    except sqlite3.OperationalError as exc:
        core.log(f"incidents: could not persist ({exc}); will re-observe")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0
    return n


def evaluate(conn, signal: str, entity: str = "", value: float | None = None,
             wall: float | None = None) -> int:
    """Convenience for probes: feed a sample and persist whatever it triggers."""
    return apply(conn, detect.evaluate(signal, entity, value, wall))
