"""Ship new rows (delta by ascending id) from the node's local DB to the hub's /ingest.
A local ship_state(table_name,last_id) cursor advances only on HTTP 200; the hub is
idempotent (UNIQUE(node,src_id)) so optimistic advancement is safe.

Default: drain once and exit (for a timer). Set SMOKEMON_SHIP_INTERVAL>0 to loop."""

import gzip
import ipaddress
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import config, core, schema

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


# ext_events / log_excerpts carry the "what broke" signal; gather them before the bulk metric
# tables so under a backlog they ride the earliest batch (and the hub commits them first).
_SHIP_PRIORITY = ("ext_events", "log_excerpts")


def _ordered_tables() -> tuple[str, ...]:
    """Std tables in ship order (priority tables first), minus any in config.SHIP_EXCLUDE. Excluded
    tables are still collected/kept node-local; they are just never gathered for a push. ping_rtts
    is not a STD_TABLE and is gated separately by SHIP_RTTS, so excluding it here is a no-op."""
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
        if rows:
            payload[t] = {"columns": cols, "rows": [list(r) for r in rows]}
            maxids[t] = rows[-1][cols.index("id")]
    # ping_rtts ships keyed to already-shipped ping_runs (run_id <= their max).
    # Off by default (SHIP_RTTS): the hub reads percentiles from ping_runs, not raw rtts.
    runs_cap = maxids.get("ping_runs", _last(conn, dest, "ping_runs"))
    rtt_last = _last(conn, dest, "ping_rtts")
    if config.SHIP_RTTS and runs_cap > rtt_last:
        try:
            rrows = conn.execute("SELECT run_id, rtt_ms FROM ping_rtts WHERE run_id>? AND run_id<=? ORDER BY run_id",
                                 (rtt_last, runs_cap)).fetchall()
            if rrows:
                payload["ping_rtts"] = {"columns": ["run_id", "rtt_ms"], "rows": [list(r) for r in rrows]}
            maxids["ping_rtts"] = runs_cap
        except sqlite3.OperationalError:
            pass
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
    sig = tuple((t, _last(conn, dest, t)) for t in schema.STD_TABLES)
    return sig + (("ping_rtts", _last(conn, dest, "ping_rtts")),)


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
                if "ping_rtts" in maxids:  # only a cursor advance, nothing to send
                    for _u, _s, dest in members:
                        _set_last(conn, dest, "ping_rtts", maxids["ping_rtts"])
                    conn.commit()
                continue  # group drained
            body = _compress({"node": config.NODE, "tables": payload})
            rows_n = sum(len(t["rows"]) for t in payload.values())
            # ping_rtts is capped to already-shipped runs, not SHIP_BATCH, so exclude it: if no
            # std table filled a full batch, this hub's backlog is drained after this round.
            more = any(len(v["rows"]) >= config.SHIP_BATCH for k, v in payload.items() if k != "ping_rtts")
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
    """One-shot drain triggered out-of-band (by the collector when an elevated event lands) so
    errors reach the hub without waiting for the bulk ship timer. No-op without configured/valid
    hubs. Safe to run concurrently with the timer shipper: the hub is idempotent on
    UNIQUE(node, src_id) and per-dest cursors converge."""
    if not config.HUBS:
        return 0
    valid = valid_hubs(config.HUBS)
    return connect_and_drain(valid) if valid else 0


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
