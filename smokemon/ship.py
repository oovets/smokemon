"""Ship new rows (delta by ascending id) from the node's local DB to the hub's /ingest.
A local ship_state(table_name,last_id) cursor advances only on HTTP 200; the hub is
idempotent (UNIQUE(node,src_id)) so optimistic advancement is safe.

This module owns both halves of shipping: the transport (gather/compress/post) and the
decision of when to run it (tick). Keeping them together is deliberate -- there must be
exactly one shipping mechanism, or two of them race for the same cursors."""

import gzip
import ipaddress
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import config, core, schema
from .probes.logexcerpt import is_elevated

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# Tailscale's address ranges: CGNAT IPv4 (100.64.0.0/10) and the IPv6 ULA prefix. Traffic to a
# tailnet address is WireGuard-encrypted end to end, so http:// to it does NOT expose the shared
# secret - it's an encrypted transport just like https, and is the project's actual fleet path.
_TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _is_tailscale(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname, not a literal IP - can't tell, fall through to the other rules
    return any(ip in net for net in _TAILSCALE_NETS)


def hub_url_ok(url: str) -> tuple[bool, str]:
    """The shipper authenticates with a shared secret carried in a plaintext header, so the
    transport must be encrypted or the secret leaks. Allow https, loopback (local testing), a
    Tailscale address (the tailnet is WireGuard-encrypted), or an explicit SMOKEMON_HUB_INSECURE=1
    (other trusted LAN). Returns (ok, reason)."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True, "https"
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return True, "loopback"
    if _is_tailscale(host):
        return True, "tailscale"
    if config.HUB_INSECURE:
        return True, "insecure-allowed"
    return False, (f"refusing to ship over {parsed.scheme or 'unknown'}:// to {host or '?'} - the "
                   "shared secret would be sent in clear. Use https, a Tailscale (100.64/10) hub "
                   "address, or set SMOKEMON_HUB_INSECURE=1.")


def init_state(conn: sqlite3.Connection) -> None:
    """Ensure ship_state has the per-destination composite key (dest, table_name). Migrates an
    old single-cursor table (table_name PRIMARY KEY) in one transaction, mapping its rows to the
    primary hub's dest. Idempotent on fresh and already-migrated DBs. Finally drops cursor rows
    for destinations that are no longer configured (only when hubs are configured) so a repointed
    or removed hub can't leave a stale cursor that skews prune's MAX."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ship_state)").fetchall()]
    if not cols:  # fresh DB
        conn.execute("CREATE TABLE ship_state (dest TEXT NOT NULL, table_name TEXT NOT NULL, "
                     "last_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (dest, table_name))")
        conn.commit()
    elif "dest" not in cols:  # old schema -> migrate atomically
        primary = config.hub_dest(config.HUBS[0][0]) if config.HUBS else "legacy"
        conn.execute("BEGIN")  # explicit: legacy isolation doesn't wrap DDL, so make it atomic
        conn.execute("ALTER TABLE ship_state RENAME TO ship_state_old")
        conn.execute("CREATE TABLE ship_state (dest TEXT NOT NULL, table_name TEXT NOT NULL, "
                     "last_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (dest, table_name))")
        conn.execute("INSERT INTO ship_state (dest, table_name, last_id) "
                     "SELECT ?, table_name, last_id FROM ship_state_old", (primary,))
        conn.execute("DROP TABLE ship_state_old")
        conn.commit()
    if config.HUBS:  # orphan cleanup: forget cursors for destinations no longer configured
        keep = [config.hub_dest(u) for u, _ in config.HUBS]
        conn.execute(f"DELETE FROM ship_state WHERE dest NOT IN ({','.join('?' * len(keep))})", keep)
        conn.commit()


def _last(conn, dest: str, table: str) -> int:
    row = conn.execute("SELECT last_id FROM ship_state WHERE dest=? AND table_name=?",
                       (dest, table)).fetchone()
    return row[0] if row else 0


def _set_last(conn, dest: str, table: str, value: int) -> None:
    conn.execute("INSERT INTO ship_state (dest,table_name,last_id) VALUES (?,?,?) "
                 "ON CONFLICT(dest,table_name) DO UPDATE SET last_id=excluded.last_id",
                 (dest, table, value))


# Latency optimisation ONLY: under a backlog these ride the earliest batch, so the hub learns
# what broke before it learns the surrounding detail. Correctness does not depend on the order --
# uid is a content key, not a rowid, so a sample that arrives before its parent transition is
# valid-but-unjoined (query.load_incident_samples reads it standalone) rather than broken.
_SHIP_PRIORITY = ("incidents", "incident_samples", "ext_events", "log_excerpts", "heartbeats")


def _ordered_tables() -> tuple[str, ...]:
    """Std tables in ship order (priority tables first), minus any in config.SHIP_EXCLUDE. Excluded
    tables are still collected/kept node-local; they are just never gathered for a push."""
    excl = config.SHIP_EXCLUDE
    priority = tuple(t for t in _SHIP_PRIORITY if t in schema.STD_TABLES and t not in excl)
    rest = tuple(t for t in schema.STD_TABLES if t not in _SHIP_PRIORITY and t not in excl)
    return priority + rest


def gather(conn, dest: str) -> tuple[dict, dict]:
    payload: dict[str, dict] = {}
    maxids: dict[str, int] = {}
    for t in _ordered_tables():
        last = _last(conn, dest, t)
        try:
            cur = conn.execute(f"SELECT * FROM {t} WHERE id>? ORDER BY id LIMIT ?", (last, config.SHIP_BATCH))
        except sqlite3.OperationalError:
            continue  # table not present on this node yet
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            continue
        maxids[t] = rows[-1][cols.index("id")]
        payload[t] = {"columns": cols, "rows": [list(r) for r in rows]}
    return payload, maxids


def _compress(payload: dict) -> bytes:
    # gzip the body: numeric row-JSON compresses ~5-10x. level 3 captures almost all of the
    # ratio for sub-millisecond CPU on Pi-class hardware. The hub decompresses by header, so
    # an old hub (which would 500 on a gzipped body) is the only incompatibility - both ends
    # ship together. The hub's 413/MAX_BODY guard then applies to the compressed size.
    return gzip.compress(json.dumps(payload).encode(), compresslevel=3)


def _post_body(url: str, secret: str, body: bytes) -> bool:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                 "X-Smokemon-Key": secret})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        core.log(f"POST to {url} failed: {e!r}")
        return False


def _frontier(conn, dest: str) -> tuple:
    """Signature of everything gather() reads for a dest, so hubs at an identical cursor position
    can share one gather + one compressed body (the fan-out CPU-1x optimization)."""
    return tuple((t, _last(conn, dest, t)) for t in schema.STD_TABLES)


def drain(conn, hubs: list[tuple[str, str]] | None = None) -> int:
    """Fan out to every hub: each gets a full copy. Hubs sharing a cursor frontier are gathered
    and gzipped ONCE and the same body is POSTed to all of them (CPU ~1x, egress xN). A hub that
    has lagged behind (was unreachable) gets its own gather/compress. A failed POST leaves that
    hub's cursor untouched and drops it for the rest of this drain (retried next run) - it never
    blocks or rolls back another hub. Returns total rows shipped (summed across hubs)."""
    hubs = config.HUBS if hubs is None else hubs
    total = 0
    active = [(u, s, config.hub_dest(u)) for u, s in hubs]
    while active and not core.stopping():
        groups: dict[tuple, list] = {}
        for h in active:
            groups.setdefault(_frontier(conn, h[2]), []).append(h)
        next_active: list = []
        for members in groups.values():
            payload, maxids = gather(conn, members[0][2])  # identical frontier -> identical payload
            if not payload:
                continue  # group drained
            body = _compress({"node": config.NODE, "tables": payload})
            rows_n = sum(len(t["rows"]) for t in payload.values())
            # No table filled a full batch -> this hub's backlog is drained after this round.
            more = any(len(v["rows"]) >= config.SHIP_BATCH for v in payload.values())
            for u, s, dest in members:
                if not _post_body(u, s, body):
                    continue  # cursor unchanged; drop from active this drain
                for t, mid in maxids.items():
                    _set_last(conn, dest, t, mid)
                conn.commit()
                total += rows_n
                if more:
                    next_active.append((u, s, dest))
        active = next_active
    return total


def valid_hubs(hubs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop hubs whose URL would leak the shared secret in clear (per hub_url_ok), logging each.
    Availability over strictness: one misconfigured hub must not stop shipping to the good ones."""
    out: list[tuple[str, str]] = []
    for u, s in hubs:
        ok, reason = hub_url_ok(u)
        if ok:
            out.append((u, s))
        else:
            core.log(f"ship: skipping hub {u}: {reason}")
    return out


def connect_and_drain(hubs: list[tuple[str, str]]) -> int:
    """Open the node DB, ensure ship_state, drain to `hubs`, close. Shared by the one-shot ship
    path and by expedite() so both go through the identical migration + fan-out logic."""
    conn = core.connect(config.DB_PATH)
    try:
        init_state(conn)
        return drain(conn, hubs)
    finally:
        conn.close()


def expedite() -> int:
    """One-shot drain on its own connection, for the shipping thread. No-op without
    configured/valid hubs."""
    if not config.HUBS:
        return 0
    valid = valid_hubs(config.HUBS)
    return connect_and_drain(valid) if valid else 0


# ---------- when to ship ----------
#
# One mechanism, in the module that owns shipping. This used to be a systemd timer firing a
# fresh `python -m smokemon.ship` every 15 s -- 5760 interpreter startups a day, ~200 ms each
# on Pi-class hardware, purely to import modules and usually find nothing to send. For an agent
# whose entire claim is a small footprint, that was the largest thing it did.
#
# Now the collector calls tick() on its scheduler. Shipping still happens on a thread, because
# a POST to an unreachable hub blocks for the socket timeout and must never stall sampling.

_seen_id: int | None = None      # high-water mark of ext_events already examined
_pending = False                 # something is waiting to be shipped (a LEVEL, not an edge)
_next_due = 0.0                  # monotonic deadline for the periodic ship
_inflight = threading.Lock()     # coalesce: at most one ship in flight


def _elevated_pending(conn) -> bool:
    """Raise the pending flag if an elevated ext_events row has appeared since the last look.

    A level rather than an edge: a correlated storm -- a thermal throttle tripping temperature,
    loss and latency at once -- raises it once and costs one ship, not one per incident. It is
    cleared only when a ship actually starts, so a detection arriving mid-flight is not lost;
    the running ship's gather() may already have passed those rows.

    The first call only seeds the mark, so a pre-existing backlog is not expedited on startup."""
    global _seen_id, _pending
    row = conn.execute("SELECT COALESCE(MAX(id),0) FROM ext_events").fetchone()
    cur_max = int(row[0]) if row else 0
    first = _seen_id is None
    prev = _seen_id or 0
    _seen_id = cur_max
    if first or cur_max <= prev:
        return _pending
    for source, sev in conn.execute(
            "SELECT source, severity FROM ext_events WHERE id>? ORDER BY id", (prev,)):
        # The collector's own events (probe-crash / db-contention) must NOT trigger a ship: a
        # ship is another local writer, so reacting to a local contention event would add write
        # pressure and feed a crash->ship->crash loop. Those ride the periodic tick.
        if source == "collector":
            continue
        if is_elevated(sev):
            _pending = True
            break
    return _pending


def _run() -> None:
    try:
        n = expedite()
        if n:
            core.log(f"ship: sent {n} rows")
    except Exception as e:  # noqa: BLE001 -- a thread that dies silently stops all shipping
        core.log(f"ship: failed: {e!r}")
    finally:
        try:
            _inflight.release()
        except RuntimeError:
            pass


def tick(conn) -> None:
    """Collector hook. Ships when an elevated event is waiting or the periodic deadline has
    passed, at most one at a time, never blocking the caller."""
    global _pending, _next_due
    if not config.HUBS:
        return
    now = time.monotonic()
    due = now >= _next_due
    if not (_elevated_pending(conn) or due):
        return
    if not _inflight.acquire(blocking=False):
        return  # already shipping; _pending stays raised so the next tick re-arms if needed
    _pending = False
    _next_due = now + config.SHIP_INTERVAL
    threading.Thread(target=_run, name="smokemon-ship", daemon=True).start()


def main() -> int:
    if not config.HUBS:
        core.log("ship: no hub configured (SMOKEMON_HUB_URL / SMOKEMON_HUB_URLS unset), nothing to do")
        return 0
    valid = valid_hubs(config.HUBS)
    if not valid:  # every configured hub failed validation - surface loudly (matches old exit 2)
        core.log("ship: no valid hubs after transport validation")
        return 2
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    init_state(conn)
    urls = [u for u, _ in valid]

    def once() -> None:
        n = drain(conn, valid)
        if n:
            core.log(f"shipped {n} rows to {len(valid)} hub(s)")

    if config.SHIP_INTERVAL > 0:
        core.log(f"ship daemon: node={config.NODE} hubs={urls} interval={config.SHIP_INTERVAL}s")
        core.run_scheduler([(config.SHIP_INTERVAL, once)])  # runs now, then every interval
    else:
        core.log(f"ship once: node={config.NODE} hubs={len(valid)} shipped {drain(conn, valid)} rows")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
