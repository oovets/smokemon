"""Daemon runtime shared by all collectors: logging, DB connect, signals, scheduler."""

import hashlib
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable

from . import config

_stop = threading.Event()


def _jitter(interval: float, node: str = "") -> float:
    """Stable per-node offset in [0, interval/4) so the fleet doesn't ping/ship in lockstep.
    Wall-clock-aligned cadence makes every node fire on the same boundary; a hash-derived
    offset (sha1 of node name, so it's stable across restarts and unsalted unlike hash())
    spreads the herd out without drifting. interval<=0 (or no node) -> no offset."""
    if interval <= 0 or not node:
        return 0.0
    h = int(hashlib.sha1(node.encode()).hexdigest()[:8], 16)
    return (h % 10_000) / 10_000.0 * (interval / 4.0)


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# Bumped whenever the on-disk layout changes incompatibly. A database written by an older
# schema is set aside rather than migrated: the incident pivot removed ~20 tables whose rows
# have no meaning under the new model, so migrating them would mean carrying a translation
# layer for data nothing will ever read.
SCHEMA_VERSION = 2


def _set_aside_if_stale(path: str) -> None:
    """Move a database written by an incompatible older schema out of the way.

    Renames rather than deletes. The old rows are worthless under the new model, but a tool
    whose whole purpose is explaining incidents should not contain a code path that silently
    removes a database -- if this ever fires on something the operator did care about, the
    file is still sitting next to the new one."""
    if not os.path.exists(path):
        return
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return   # unreadable or not a database; let the normal open path report it
    try:
        found = probe.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        found = 0
    finally:
        probe.close()
    if found >= SCHEMA_VERSION:
        return
    aside = f"{path}.old-v{found}"
    for suffix in ("", "-wal", "-shm"):
        try:
            if os.path.exists(path + suffix):
                os.replace(path + suffix, aside + suffix)
        except OSError as e:
            log(f"schema: could not set aside {path + suffix}: {e}")
            return
    log(f"schema: database was v{found}, need v{SCHEMA_VERSION}; kept previous file at {aside}")


def connect(path: str, timeout: float = 30, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection with the minimal set of PRAGMAs that buy real value
    without inflating RSS. Footprint matters: smokemon's own RSS is one of the metrics
    it reports (host.py reads /proc/<pid>/stat field 21), so cache_size / mmap_size
    tuning would both bloat the number and confuse the report. Defaults SQLite picks
    for cache_size (~2 MB) are already fine for our workload."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _set_aside_if_stale(path)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")     # concurrent read while a writer holds the file
    conn.execute("PRAGMA synchronous=NORMAL")   # half the fsyncs of FULL; still durable on WAL
    conn.execute("PRAGMA busy_timeout=10000")   # 10s grace before reads/writes raise OperationalError
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


def connect_ro(path: str, timeout: float = 30) -> sqlite3.Connection:
    """Open a read-only connection (URI mode=ro). On the hub this serves dashboard/API GETs
    so they read concurrently under WAL instead of queuing behind ingest on the writer's lock.
    query_only is belt-and-braces: the connection cannot mutate even if asked. The DB must
    already exist (the writer creates it); shared across threads, so callers serialize use."""
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def install_signals() -> None:
    def handler(signum, _frame):
        _stop.set()
        log(f"signal {signum} received, exiting after current cycle")
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def stopping() -> bool:
    return _stop.is_set()


def run_scheduler(probes: list[tuple[float, Callable[[], None]]]) -> None:
    """Run each (interval, fn) on its own wall-clock-aligned cadence in this thread.
    A failing probe is logged and never kills the loop. Each cadence carries a stable
    per-node jitter offset so the whole fleet doesn't fire on the same boundary."""
    offsets = [_jitter(interval, config.NODE) for interval, _ in probes]
    due = {i: 0.0 for i in range(len(probes))}
    while not _stop.is_set():
        for i, (interval, fn) in enumerate(probes):
            # Re-read the clock per probe. Reading it once per pass meant that if the group's
            # probes together overran their interval, every subsequent `due` was computed from
            # an already-stale `now`, landed in the past, and re-fired immediately with dt~=0 --
            # which the rate-derived metrics in host.py divide by, producing phantom CPU and
            # disk-IO spikes indistinguishable from a real incident.
            now = time.time()
            if now >= due[i]:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    log(f"probe error: {e!r}")
                off = offsets[i]
                # Compute the next slot from the time the probe *finished*, so a run that
                # overran its interval skips the missed slot instead of re-firing at once.
                # Still wall-clock-aligned: the result is always k*interval + off.
                done = time.time()
                due[i] = (int((done - off) // interval) + 1) * interval + off
        nxt = min(due.values())
        while not _stop.is_set():
            d = nxt - time.time()
            if d <= 0:
                break
            _stop.wait(min(d, 1.0))
