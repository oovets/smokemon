"""Agent liveness and slow-trend summary. The only row a healthy node writes.

This is agent core, not a probe: it does not observe the host the way ping or temperature do,
it reports on the agent itself and on the few quantities that need a continuous series no
matter how quiet things are.

It exists because silence is ambiguous. Once normal operation stops reaching disk, a node with
nothing to say looks exactly like a node that died, and the hub has no way to tell the
difference. `interval_s` is carried in the row so the hub derives staleness from what the node
actually does rather than from hub-side config -- one node may run a slower heartbeat without
being declared dead.

It also carries the handful of signals that only make sense as a trend: disk headroom and SD
wear (death clocks measured in weeks, useless from incident windows alone) and the agent's own
database size, because "smokemon fills the disk it was watching" is the failure mode this whole
design is most likely to introduce and must therefore be self-observable.

Row rate is the entire steady-state write budget of a healthy node. At the 300 s default that
is 288 rows/day against the ~345,000 the old continuous ping path wrote.
"""

from __future__ import annotations

import os
import time

from . import __version__, config, core, incidents, schema, signals

_started_wall = time.time()
_started_mono = time.monotonic()


def _sizes(path: str) -> tuple[float | None, float | None]:
    """(db_mb, wal_mb). The WAL is reported separately because it is the part that grows
    between checkpoints, and a WAL that stops shrinking is the early sign of a reader holding
    a transaction open."""
    def mb(p: str) -> float | None:
        try:
            return round(os.path.getsize(p) / 1e6, 2)
        except OSError:
            return None
    return (mb(path), mb(path + "-wal"))


def _uptime_s() -> float | None:
    try:
        with open("/proc/uptime") as f:
            return round(float(f.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


def _disk(path: str) -> tuple[float | None, float | None, float | None]:
    """(free_gb, used_pct, inode_used_pct) for the filesystem holding the database -- the one
    that actually matters for the agent's own survival."""
    try:
        st = os.statvfs(os.path.dirname(path) or "/")
    except OSError:
        return (None, None, None)
    if not st.f_blocks:
        return (None, None, None)
    free_gb = round(st.f_bavail * st.f_frsize / 1e9, 2)
    used_pct = round(100.0 * (1 - st.f_bfree / st.f_blocks), 1)
    inode_pct = round(100.0 * (1 - st.f_ffree / st.f_files), 1) if st.f_files else None
    return (free_gb, used_pct, inode_pct)


def collect(conn) -> None:
    now = time.time()
    from .probes import host as hostprobe  # local: keeps import order free of probe side effects

    db_mb, wal_mb = _sizes(config.DB_PATH)
    free_gb, used_pct, inode_pct = _disk(config.DB_PATH)
    n_signals, sig_bytes = signals.stats()
    h = hostprobe.last()

    schema.insert(conn, "heartbeats", [{
        "ts": now,
        "interval_s": config.HEARTBEAT_INTERVAL,
        "uptime_s": _uptime_s(),
        # Rises monotonically while the agent stays up, so a restart is visible as a reset.
        # This is the ONLY thing that catches a crash loop restarting faster than any rule's
        # for_s -- such a node never keeps a candidate long enough to open an incident and
        # would otherwise look perfectly healthy.
        "agent_uptime_s": round(time.monotonic() - _started_mono, 1),
        "db_mb": db_mb, "wal_mb": wal_mb,
        "disk_free_gb": free_gb, "disk_used_pct": used_pct, "inode_used_pct": inode_pct,
        "write_mb_day": h.get("write_mb_day"), "wear_pct": h.get("wear_pct"),
        "rss_mb": h.get("rss_mb"), "cpu_pct": h.get("cpu_pct"),
        "load1": h.get("load1"),
        "mem_used_pct": h.get("mem_used_pct"),
        "swap_used_pct": h.get("swap_used_pct"),
        "temp_c": h.get("temp_c"),
        "throttle_bits": h.get("throttle_bits"),
        "open_incidents": incidents.open_count(conn),
        # A node whose detector has gone quiet because a probe stopped feeding signals looks
        # identical to a healthy one. These two make that distinguishable, and prove the
        # memory bound in production rather than only in a test.
        "signals": n_signals, "signal_kb": round(sig_bytes / 1024.0, 1),
        "signal_drops": signals.drops(),
        "ver": __version__,
    }])
    conn.commit()

    if signals.should_warn_drops(now):
        core.log(f"signals: registry full at {config.SIGNAL_MAX}; "
                 f"{signals.drops()} feeds dropped -- coverage is incomplete")
