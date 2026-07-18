"""Device & environment inventory, delta-coded (vslow tier).

Captures the slow-moving facts about a node and its environment - model, kernel, OS release,
JetPack/L4T, CPU/mem, network interfaces, gateway, boot id - so the fleet view can answer
"what exactly is this box and what's it running" without log streaming. Each fact is written
to device_facts only when its value changes from the last recorded value, so steady-state cost
is one /proc + /sys scan per hour that usually emits zero rows.

Stdlib only; reads /proc + /sys. Best-effort: any unreadable fact is simply omitted."""

from __future__ import annotations

import os
import platform
import sqlite3
import time

from .. import adapters, config, schema

_loaded = False           # have we seeded _last from the DB yet (once per process)?
_last: dict[str, str] = {}  # key -> last emitted value, for delta-coding


def _read(path: str, max_bytes: int = 4096) -> str | None:
    """First NUL-delimited token of a small file, stripped. device-tree nodes are NUL-terminated."""
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return None
    text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    return text or None


def _os_pretty() -> str | None:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip() == "PRETTY_NAME" and v:
                    return v.strip().strip('"')
    except OSError:
        pass
    return None


def _model() -> str | None:
    for p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        m = _read(p)
        if m:
            return m
    return None


def _mem_total_mb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _interfaces() -> str | None:
    try:
        names = [n for n in sorted(os.listdir("/sys/class/net")) if n != "lo"]
    except OSError:
        return None
    return ",".join(names) or None


def _gather() -> list[tuple[str, str, str]]:
    """[(key, value, kind)] of currently-observed facts; missing ones are dropped."""
    facts: list[tuple[str, str, str]] = []

    def add(key: str, value, kind: str) -> None:
        if value is not None and str(value) != "":
            facts.append((key, str(value), kind))

    u = platform.uname()
    add("hostname", config.NODE, "runtime")
    add("python", platform.python_version(), "runtime")
    add("arch", u.machine, "runtime")
    add("boot_id", _read("/proc/sys/kernel/random/boot_id"), "runtime")
    add("kernel", u.release, "os")
    add("kernel_version", u.version, "os")
    add("os", _os_pretty() or u.system, "os")
    add("l4t", _read("/etc/nv_tegra_release"), "os")  # Jetson JetPack/L4T release line
    add("model", _model(), "hw")
    add("cpu_count", os.cpu_count(), "hw")
    add("mem_total_mb", _mem_total_mb(), "hw")
    add("interfaces", _interfaces(), "net")
    add("gateway", config.default_gateway(), "net")
    add("tailscale_iface", adapters.detect_tailscale_iface(), "net")
    return facts


def _seed_from_db(conn) -> None:
    """Load the last value per key so a restart doesn't re-emit every fact. Uses the highest
    id per key (ids rise with ts)."""
    global _loaded, _last
    try:
        rows = conn.execute(
            "SELECT key, value FROM device_facts WHERE id IN "
            "(SELECT MAX(id) FROM device_facts GROUP BY key)").fetchall()
        _last = {k: v for k, v in rows if k is not None}
    except sqlite3.OperationalError:
        _last = {}
    _loaded = True


def collect(conn) -> None:
    if not config.INVENTORY_ENABLED:
        return
    if not _loaded:
        _seed_from_db(conn)
    now = time.time()
    rows = []
    for key, value, kind in _gather():
        if _last.get(key) != value:
            rows.append({"ts": now, "key": key, "value": value, "kind": kind})
            _last[key] = value
    if rows:
        schema.insert(conn, "device_facts", rows)
        conn.commit()
