"""Hub-side service-alert delivery (delivery-only).

A background pass run by the hub process periodically diffs the fleet's *current* service/host
degradations - the same ones the Risk tab shows (hubapi._service_alerts: gst/watched-proc down,
RTSP stream failing, docker restart-loops/dead/unhealthy/OOM, redis down, memory/throttle/
conntrack) - against a small alert_state table and pushes newly-firing and newly-resolved alerts
to SMOKEMON_NOTIFY_URL via notify.py.

Detection is reused, not reinvented: this module only adds delivery (dedup / flap-suppression /
mute / re-notify cooldown). Pure stdlib, hub-side, read-only over node data. No-op unless a
notify URL is configured. The pass is split into pure steps (evaluate / load_state / plan /
render / persist) so the hub loop can hold its DB locks around the right ones and run the
webhook POST outside any lock; the steps are also unit-testable without a server."""

import fnmatch
import time

from . import config, hubapi


def _key(a: dict) -> str:
    """Stable identity for an alert across passes: node/kind/label."""
    return f"{a['node']}/{a['kind']}/{a.get('label', '')}"


def _muted(key: str) -> bool:
    return any(fnmatch.fnmatch(key, pat) for pat in config.ALERT_MUTE)


def _allowed(key: str) -> bool:
    """True if `key` may page. With an empty NOTIFY_ALLOW the allowlist is off (everything passes);
    when set it is default-deny - only keys matching a glob page, the rest are tracked but never
    sent. Mute is applied separately and always wins."""
    allow = config.NOTIFY_ALLOW
    return (not allow) or any(fnmatch.fnmatch(key, pat) for pat in allow)


def evaluate(conn, now: float | None = None) -> dict[str, dict]:
    """key -> alert for the currently-firing alerts, severity-gated (NOTIFY_MIN_SEVERITY): the
    service/host degradations from hubapi._service_alerts (incl. node-down heartbeat) plus the
    active network incidents from hubapi._incident_alerts, so the nodes do zero extra work. NB:
    mute is *not* applied here
    - muting suppresses paging only (see to_page), while every alert is still tracked so the Risk
    tab can show its firing-since even when nothing is sent."""
    now = time.time() if now is None else now
    out: dict[str, dict] = {}
    detected = (hubapi._service_alerts(conn, config.ALERT_WINDOW_HOURS, now)
                + hubapi._incident_alerts(conn, config.ALERT_WINDOW_HOURS, now))
    for a in detected:
        if a["severity"] < config.NOTIFY_MIN_SEVERITY:
            continue
        k = _key(a)
        out[k] = {**a, "key": k}
    return out


def to_page(alerts: list[dict]) -> list[dict]:
    """Subset of an alert list that should actually be sent to the webhook: only when a notify URL
    is configured, restricted to the NOTIFY_ALLOW allowlist (if set) and excluding muted keys.
    Allowlist/mute/missing-URL suppress *paging* only - tracking (alert_state, the dashboard's
    firing-since) still covers every alert."""
    if not config.NOTIFY_URL:
        return []
    return [a for a in alerts if _allowed(a["key"]) and not _muted(a["key"])]


def load_state(conn) -> dict[str, dict]:
    """key -> tracked state for alerts currently recorded as firing (or lingering after they
    stopped being detected, until the resolve-linger window elapses)."""
    return {r[0]: {"severity": r[1], "detail": r[2], "first_ts": r[3], "notified_ts": r[4],
                   "cleared_ts": r[5]}
            for r in conn.execute(
                "SELECT key, severity, detail, first_ts, notified_ts, cleared_ts FROM alert_state")}


def plan(current: dict[str, dict], state: dict[str, dict],
         now: float) -> tuple[list[dict], list[dict]]:
    """Pure decision step. Returns (firing_to_page, resolved):
      firing_to_page - brand-new alerts, or still-firing ones past the re-notify cooldown
                       (notified_ts None means an earlier send failed -> retry now)
      resolved       - tracked keys no longer detected, AND absent long enough to clear the
                       resolve-linger window (flap suppression): a key that reappears before
                       ALERT_RESOLVE_AFTER_S elapses never resolves, so flapping conditions stay
                       one open alert instead of churning firing/resolved every pass."""
    firing = []
    for k, a in current.items():
        prev = state.get(k)
        if (prev is None or prev["notified_ts"] is None
                or now - prev["notified_ts"] >= config.ALERT_RENOTIFY_S):
            firing.append(a)
    grace = config.ALERT_RESOLVE_AFTER_S
    resolved = []
    for k in state:
        if k in current:
            continue
        cleared = state[k].get("cleared_ts")
        # absent now: resolve only once it's been continuously absent for the grace window.
        if grace <= 0 or (cleared is not None and now - cleared >= grace):
            resolved.append({"key": k, **state[k]})
    return firing, resolved


def render(firing: list[dict], resolved: list[dict]) -> tuple[str | None, str | None]:
    """(title, body) summarising this pass, or (None, None) when there is nothing to send."""
    lines = [f"[FIRING s{a['severity']}] {a['node']} {a['kind']}/{a.get('label', '')}: {a['detail']}"
             for a in sorted(firing, key=lambda a: (-a["severity"], a["key"]))]
    if config.ALERT_NOTIFY_RESOLVED:
        for a in sorted(resolved, key=lambda a: a["key"]):
            node, kind, label = (a["key"].split("/", 2) + ["", ""])[:3]
            lines.append(f"[RESOLVED] {node} {kind}/{label}")
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


def event_for(a: dict, status: str) -> dict:
    """Map one alert to incident.io event fields (deduplication_key / status / title / description /
    metadata). Pure and side-effect-free so it is unit-testable. Works for both a firing alert dict
    (carries node/kind/label/severity/detail/extra) and a resolved-state row (only the key survives
    a resolve, so node/kind/label are recovered by splitting it). The deduplication_key is _key(a),
    so a firing and its later resolve share one key and incident.io closes the alert."""
    if "node" in a:
        node, kind, label = a["node"], a["kind"], a.get("label", "")
    else:
        node, kind, label = (a["key"].split("/", 2) + ["", ""])[:3]
    title = f"smokemon: {node} {kind}/{label}".rstrip("/")
    meta: dict = {"node": node, "kind": kind}
    if label:
        meta["label"] = label
    if a.get("severity") is not None:
        meta["severity"] = a["severity"]
    for k, v in a.get("extra") or []:
        meta[str(k)] = v
    return {"dedup_key": a["key"], "status": status, "title": title,
            "description": a.get("detail", ""), "metadata": meta}


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
                "(key, node, kind, label, severity, detail, first_ts, notified_ts, cleared_ts) "
                "VALUES (?,?,?,?,?,?,?,?,NULL)",
                (k, a["node"], a["kind"], a.get("label", ""), a["severity"], a["detail"], now, notified))
        else:
            # present again -> clear any pending linger so it stays a single open alert
            conn.execute("UPDATE alert_state SET severity=?, detail=?, notified_ts=?, cleared_ts=NULL "
                         "WHERE key=?", (a["severity"], a["detail"], notified, k))
    for a in resolved:
        conn.execute("DELETE FROM alert_state WHERE key=?", (a["key"],))
    # keys tracked but no longer detected and not yet resolved: start (or keep) the linger clock.
    resolved_keys = {a["key"] for a in resolved}
    tracked = {r[0] for r in conn.execute("SELECT key FROM alert_state")}
    for k in tracked - set(current) - resolved_keys:
        conn.execute("UPDATE alert_state SET cleared_ts=? WHERE key=? AND cleared_ts IS NULL", (now, k))
    conn.commit()
