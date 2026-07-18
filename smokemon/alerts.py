"""Hub-side incident-alert delivery (delivery-only).

A background pass run by the hub process periodically diffs the fleet's currently-open incidents
(hubapi.open_incident_alerts) against a small alert_state table and pushes newly-firing and
newly-resolved alerts to SMOKEMON_NOTIFY_URL via notify.py.

Detection is reused, not reinvented: the node's detector already applied debounce, hysteresis and
dedup before it wrote the transition, so this module only adds delivery (mute / re-notify
cooldown / resolve). Pure stdlib, hub-side, read-only over node data. No-op unless a notify URL is
configured. The pass is split into pure steps (evaluate / load_state / plan / render / persist) so
the hub loop can hold its DB locks around the right ones and run the webhook POST outside any
lock; the steps are also unit-testable without a server."""

import fnmatch
import time

from . import config, hubapi


def _key(a: dict) -> str:
    """Stable identity for an alert across passes: the incident uid.

    uid is the right key precisely because it is stable for the life of one occurrence and
    changes when a genuinely new occurrence begins -- which is what ALERT_RENOTIFY_S needs to
    measure against. Keying on node/kind/label instead would re-litigate hub-side the debounce
    and hysteresis the node already applied: a signal flapping across its threshold would reuse
    one key and read as a single long alert, and a real second occurrence would be silently
    suppressed by the previous one's cooldown."""
    return a["key"]


def _muted(key: str) -> bool:
    return any(fnmatch.fnmatch(key, pat) for pat in config.ALERT_MUTE)


def evaluate(conn, now: float | None = None) -> dict[str, dict]:
    """uid -> alert for every currently-open incident, severity-gated (NOTIFY_MIN_SEVERITY).

    A projection of what the nodes already decided, so they do zero extra work. NB: mute is *not*
    applied here - muting suppresses paging only (see to_page), while every alert is still tracked
    so the dashboard can show its firing-since even when nothing is sent."""
    now = time.time() if now is None else now
    return {_key(a): a for a in hubapi.open_incident_alerts(conn, now).values()
            if a["severity"] >= config.NOTIFY_MIN_SEVERITY}


def to_page(alerts: list[dict]) -> list[dict]:
    """Subset of an alert list that should actually be sent to the webhook: only when a notify
    URL is configured, and excluding muted keys. Mute and the missing-URL case suppress *paging*
    only - tracking (alert_state, the dashboard's firing-since) still covers every alert."""
    if not config.NOTIFY_URL:
        return []
    return [a for a in alerts if not _muted(a["key"])]


def load_state(conn) -> dict[str, dict]:
    """key -> tracked state for alerts currently recorded as firing.

    node/kind/label come back too because the key is now an opaque incident uid: a resolved
    alert has left `current`, so this row is the only place left to learn what it was about."""
    return {r[0]: {"node": r[1], "kind": r[2], "label": r[3], "severity": r[4],
                   "detail": r[5], "first_ts": r[6], "notified_ts": r[7]}
            for r in conn.execute(
                "SELECT key, node, kind, label, severity, detail, first_ts, notified_ts "
                "FROM alert_state")}


def plan(current: dict[str, dict], state: dict[str, dict],
         now: float) -> tuple[list[dict], list[dict]]:
    """Pure decision step. Returns (firing_to_page, resolved):
      firing_to_page - brand-new alerts, or still-firing ones past the re-notify cooldown
                       (notified_ts None means an earlier send failed -> retry now)
      resolved       - tracked keys no longer present in `current`."""
    firing = []
    for k, a in current.items():
        prev = state.get(k)
        if prev is None:
            firing.append(a)
        elif prev["notified_ts"] is None or now - prev["notified_ts"] >= config.ALERT_RENOTIFY_S:
            firing.append(a)
    resolved = [{"key": k, **state[k]} for k in state if k not in current]
    return firing, resolved


def render(firing: list[dict], resolved: list[dict]) -> tuple[str | None, str | None]:
    """(title, body) summarising this pass, or (None, None) when there is nothing to send."""
    lines = [f"[FIRING s{a['severity']}] {a['node']} {a['kind']}/{a.get('label', '')}: {a['detail']}"
             for a in sorted(firing, key=lambda a: (-a["severity"], a["key"]))]
    if config.ALERT_NOTIFY_RESOLVED:
        for a in sorted(resolved, key=lambda a: a["key"]):
            lines.append(f"[RESOLVED] {a.get('node', '?')} "
                         f"{a.get('kind', '?')}/{a.get('label', '')}")
    if not lines:
        return None, None
    if firing:
        worst = max(firing, key=lambda a: a["severity"])
        title = f"smokemon: {worst['node']} {worst['kind']}/{worst.get('label', '')}"
        if len(firing) > 1:
            title += f" (+{len(firing) - 1} more)"
    else:
        title = f"smokemon: {len(resolved)} alert(s) resolved"
    return title, "\n".join(lines)


def persist(conn, current: dict[str, dict], resolved: list[dict],
            notified_keys: set, now: float) -> None:
    """Upsert firing keys, stamp notified_ts on the ones we just paged, drop resolved keys.
    The caller holds the hub write lock; this does no network I/O."""
    for k, a in current.items():
        row = conn.execute("SELECT notified_ts FROM alert_state WHERE key=?", (k,)).fetchone()
        notified = now if k in notified_keys else (row[0] if row else None)
        if row is None:
            conn.execute(
                "INSERT INTO alert_state "
                "(key, node, kind, label, severity, detail, first_ts, notified_ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (k, a["node"], a["kind"], a.get("label", ""), a["severity"], a["detail"], now, notified))
        else:
            conn.execute("UPDATE alert_state SET severity=?, detail=?, notified_ts=? WHERE key=?",
                         (a["severity"], a["detail"], notified, k))
    for a in resolved:
        conn.execute("DELETE FROM alert_state WHERE key=?", (a["key"],))
    conn.commit()
