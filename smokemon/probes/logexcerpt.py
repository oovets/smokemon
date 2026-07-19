"""Event-driven log excerpts (opt-in, OFF by default; slow tier).

Rather than streaming logs - which AGENTS.md forbids and which would dominate both disk and
wire - this ships a *capped, redacted tail* of the configured log files, and only when something
interesting just happened: a warn/error+ row landed in ext_events since the last check (governor
sheds, probe anomalies, operator events). Steady state with no incidents is a single cheap
"any new elevated events?" query and zero file reads.

Bounding, per the ADR:
  (a) byte-offset cursor per file (node-local log_cursors table) so the same bytes never ship
      twice; on rotation/truncation (file shorter than the cursor) the cursor resets to 0.
  (b) gzip - handled by the shipper, which gzips the whole batch; excerpts are stored as
      redacted text rather than double-compressed blobs that would fight that compression.
  (c) redaction of secrets/tokens before the text is ever written to the DB.
  (d) hard per-excerpt byte cap with drop-oldest (keep the freshest tail), so one noisy file
      can't blow the footprint.

On first sight of a file the cursor is seeded to the current EOF, so enabling the probe never
dumps pre-existing history - only lines written after it started watching.
"""

from __future__ import annotations

import os
import re
import time

from .. import config, schema

# Trigger on anything more serious than routine chatter. ext_events.severity is free text, so we
# treat everything outside this allow-list (warn/error/crit/numeric levels) as an incident.
_QUIET_SEVERITIES = {"", "info", "debug", "notice", "trace"}


def is_elevated(severity: str | None) -> bool:
    """True for warn/error/crit/numeric/unknown - anything outside the routine-chatter allow-list.
    Shared so the log-excerpt trigger and the ship-expedite check agree on what counts as 'broke'."""
    return (severity or "").strip().lower() not in _QUIET_SEVERITIES

# Bearer/Basic/Token <credential> -> drop the credential but keep the scheme word for context.
_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]+")
# key=value / key: value secrets in log lines. Value is replaced, the key kept for context.
_KV_RE = re.compile(
    r"(?i)\b(authorization|tokens?|secrets?|passwords?|passwd|api[-_]?key|"
    r"x-smokemon-key|aws_[a-z_]*key[a-z_]*)\b(\s*[:=]\s*)(\"?[^\s\"'&]+\"?)")

_last_event_id: int | None = None  # high-water mark of ext_events we've already reacted to


def _redact(text: str) -> str:
    """Strip obvious secrets so an incident excerpt is safe to ship. Redacts Bearer/Basic
    credentials, the value half of key=value/secret-ish pairs, and the configured hub secret
    verbatim. Best-effort, not a DLP - the point is to not casually leak the obvious tokens."""
    text = _SCHEME_RE.sub(lambda m: f"{m.group(1)} ***", text)
    text = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    sec = config.HUB_SECRET
    if sec and sec != "changeme":
        text = text.replace(sec, "***")
    return text


def _ensure_cursor_table(conn) -> None:
    # Node-local only: not in STD_TABLES, so the shipper never sends cursor state to the hub.
    conn.execute("CREATE TABLE IF NOT EXISTS log_cursors ("
                 "path TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER NOT NULL)")


def _get_cursor(conn, path: str) -> tuple[int, int]:
    """(offset, size) for path. First sight seeds both to the current EOF so we tail, not dump."""
    row = conn.execute("SELECT offset, size FROM log_cursors WHERE path=?", (path,)).fetchone()
    if row:
        return int(row[0]), int(row[1])
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    conn.execute("INSERT OR REPLACE INTO log_cursors (path, offset, size) VALUES (?,?,?)",
                 (path, size, size))
    return size, size


def _set_cursor(conn, path: str, offset: int, size: int) -> None:
    conn.execute("INSERT OR REPLACE INTO log_cursors (path, offset, size) VALUES (?,?,?)",
                 (path, offset, size))


def _read_tail(path: str, offset: int, prev_size: int) -> tuple[str | None, int, int, int]:
    """Read new bytes from `offset` to EOF, capped at LOGEXCERPT_MAX_BYTES with drop-oldest.
    Returns (text|None, new_offset, dropped_bytes, size). Handles rotation/truncation by
    resetting to 0 when the file is now shorter than where we last were."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, offset, 0, prev_size
    if size < offset or size < prev_size:  # rotated or truncated -> start over from the top
        offset = 0
    if size <= offset:                     # nothing new
        return None, size, 0, size
    start, dropped = offset, 0
    cap = config.LOGEXCERPT_MAX_BYTES
    if cap > 0 and size - start > cap:     # keep only the freshest `cap` bytes
        dropped = (size - cap) - start
        start = size - cap
    try:
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(size - start)
    except OSError:
        return None, offset, 0, size
    return raw.decode("utf-8", "replace"), size, max(0, dropped), size


def _trigger(conn) -> tuple[bool, str, str | None]:
    """(should_capture, reason, uid). True when a new elevated ext_events row appeared since the
    last check, or when SMOKEMON_LOGEXCERPT_ALWAYS is set. First call only seeds the high-water
    mark (so we don't react to pre-existing events on startup).

    uid is carried straight off the triggering row: incident-open/-close events already carry
    the exact incident uid, everything else (oom-kill, probe-crash, ...) carries whatever
    incidents.active_uid() resolved to when it fired, or None. Copying it through here -- rather
    than re-querying incident_state at capture time -- keeps the excerpt attributed to the
    condition that actually triggered it, not to whatever happens to be open a cycle later."""
    global _last_event_id
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM ext_events").fetchone()
    cur_max = int(row[0]) if row else 0
    first = _last_event_id is None
    prev = cur_max if first else _last_event_id
    _last_event_id = cur_max
    if config.LOGEXCERPT_ALWAYS:
        return True, "always", None
    if first:
        return False, "", None
    reason, uid = "", None
    for _id, source, event, severity, ev_uid in conn.execute(
            "SELECT id, source, event, severity, uid FROM ext_events WHERE id > ? ORDER BY id",
            (prev,)).fetchall():
        if not is_elevated(severity):
            continue
        reason = f"ext_event:{source or '?'}/{event or severity or '?'}"  # keep the latest match
        uid = ev_uid
    return bool(reason), reason, uid


def _name(path: str) -> str:
    """Short logical source name, e.g. /var/log/syslog -> syslog."""
    base = os.path.basename(path.rstrip("/")) or path
    return base.rsplit(".", 1)[0] if "." in base else base


def collect(conn) -> None:
    if not config.LOGEXCERPT_ENABLED or not config.LOGEXCERPT_PATHS:
        return
    _ensure_cursor_table(conn)
    # Seed every cursor on the first pass (at the current EOF) so we tail from probe start, not
    # from the first incident - otherwise lines written between enable and the triggering event
    # would be seeded away and lost. No-op once a cursor row exists.
    for path in config.LOGEXCERPT_PATHS:
        _get_cursor(conn, path)
    fire, reason, uid = _trigger(conn)
    if not fire:
        conn.commit()  # persist the first-run cursor seed
        return
    now = time.time()
    rows = []
    for path in config.LOGEXCERPT_PATHS:
        offset, prev_size = _get_cursor(conn, path)
        text, new_off, dropped, size = _read_tail(path, offset, prev_size)
        _set_cursor(conn, path, new_off, size)
        if not text:
            continue
        text = _redact(text)
        rows.append({"ts": now, "source": _name(path), "path": path, "reason": reason,
                     "bytes": len(text.encode("utf-8", "replace")), "dropped": dropped,
                     "excerpt": text, "uid": uid})
    if rows:
        schema.insert(conn, "log_excerpts", rows)
    conn.commit()
