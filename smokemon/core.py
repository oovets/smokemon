"""Daemon runtime shared by all collectors: logging, DB connect, signals, scheduler."""

import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable

_stop = threading.Event()


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def connect(path: str, timeout: float = 30, check_same_thread: bool = True) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    A failing probe is logged and never kills the loop."""
    due = {i: 0.0 for i in range(len(probes))}
    while not _stop.is_set():
        now = time.time()
        for i, (interval, fn) in enumerate(probes):
            if now >= due[i]:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    log(f"probe error: {e!r}")
                due[i] = (int(now // interval) + 1) * interval
        nxt = min(due.values())
        while not _stop.is_set():
            d = nxt - time.time()
            if d <= 0:
                break
            _stop.wait(min(d, 1.0))
