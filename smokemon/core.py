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


def connect(path: str, timeout: float = 30, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection with the minimal set of PRAGMAs that buy real value
    without inflating RSS. Footprint matters: smokemon's own RSS is one of the metrics
    it reports (host.py reads /proc/<pid>/stat field 21), so cache_size / mmap_size
    tuning would both bloat the number and confuse the report. Defaults SQLite picks
    for cache_size (~2 MB) are already fine for our workload."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")     # concurrent read while a writer holds the file
    conn.execute("PRAGMA synchronous=NORMAL")   # half the fsyncs of FULL; still durable on WAL
    conn.execute("PRAGMA busy_timeout=10000")   # 10s grace before reads/writes raise OperationalError
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
        now = time.time()
        for i, (interval, fn) in enumerate(probes):
            if now >= due[i]:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    log(f"probe error: {e!r}")
                off = offsets[i]
                due[i] = (int((now - off) // interval) + 1) * interval + off
        nxt = min(due.values())
        while not _stop.is_set():
            d = nxt - time.time()
            if d <= 0:
                break
            _stop.wait(min(d, 1.0))
