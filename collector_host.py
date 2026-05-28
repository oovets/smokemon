#!/usr/bin/env python3
"""smokemon host collector: nodhälsa (CPU, last, minne, temp, disk, disk-IO, top-processer)
på egen kadens (default 30s). Linux: full via /proc + /sys. macOS: best-effort subset
(load + minne + diskanvändning + processer via ps; temp/disk-IO hoppas över). Ren stdlib.

Skriver: host_samples, disk_samples, proc_samples.
"""

import glob
import os
import platform
import re
import resource
import signal
import sqlite3
import subprocess
import sys
import time

import platform_adapters as pa

_SYS = platform.system()  # "Darwin" | "Linux"
NODE = pa.NODE

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")
DB_PATH = os.environ.get("SMOKEMON_DB", DEFAULT_DB)
INTERVAL = float(os.environ.get("SMOKEMON_HOST_INTERVAL", "30"))
PROC_TOPN = int(os.environ.get("SMOKEMON_PROC_TOPN", "5"))

_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = resource.getpagesize()
_WHOLE_DISK_RE = re.compile(r"(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+|nvme\d+n\d+)$")

# delta-tillstånd mellan cykler
_prev_cpu: tuple[int, int] | None = None          # (total, idle)
_prev_proc: dict[str, int] = {}                   # pid -> utime+stime (ticks)
_prev_diskio: tuple[int, int, float] | None = None  # (read_bytes, write_bytes, ts)

_running = True


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _stop(signum, _frame):
    global _running
    _running = False
    log(f"signal {signum} received, exiting after current cycle")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS host_samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            cpu_pct REAL, load1 REAL, load5 REAL, load15 REAL,
            mem_used_pct REAL, mem_total_mb REAL, temp_c REAL,
            disk_read_mbps REAL, disk_write_mbps REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_host_ts ON host_samples(ts);

        CREATE TABLE IF NOT EXISTS disk_samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            mount TEXT NOT NULL,
            used_pct REAL, free_gb REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_disk_ts ON disk_samples(ts);

        CREATE TABLE IF NOT EXISTS proc_samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            pid INTEGER, name TEXT,
            cpu_pct REAL, rss_mb REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_proc_ts ON proc_samples(ts);
        """
    )
    conn.commit()
    pa.ensure_node_column(conn, ("host_samples", "disk_samples", "proc_samples"))


# ---- Linux: /proc + /sys ----------------------------------------------------

def _cpu_linux() -> float | None:
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    pct = None
    if _prev_cpu:
        ptotal, pidle = _prev_cpu
        dtotal, didle = total - ptotal, idle - pidle
        if dtotal > 0:
            pct = round(100.0 * (dtotal - didle) / dtotal, 1)
    _prev_cpu = (total, idle)
    return pct


def _mem_linux() -> tuple[float | None, float | None]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.strip().split()[0])  # kB
    except (OSError, ValueError, IndexError):
        return (None, None)
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    if not total:
        return (None, None)
    return (round(100.0 * (1 - avail / total), 1), round(total / 1024))


def _temp_linux() -> float | None:
    temps = []
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(p) as f:
                temps.append(int(f.read().strip()) / 1000.0)
        except (OSError, ValueError):
            continue
    return round(max(temps), 1) if temps else None


def _diskio_linux(ts: float) -> tuple[float | None, float | None]:
    """Aggregerad disk-throughput i MB/s (delta över hela diskar, ej partitioner)."""
    global _prev_diskio
    rb = wb = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) < 14 or not _WHOLE_DISK_RE.match(p[2]):
                    continue
                rb += int(p[5]) * 512   # sektorer lästa
                wb += int(p[9]) * 512   # sektorer skrivna
    except (OSError, ValueError):
        return (None, None)
    res: tuple[float | None, float | None] = (None, None)
    if _prev_diskio:
        pr, pw, pt = _prev_diskio
        dt = ts - pt
        if dt > 0:
            res = (round((rb - pr) / 1e6 / dt, 2), round((wb - pw) / 1e6 / dt, 2))
    _prev_diskio = (rb, wb, ts)
    return res


def _mounts_linux() -> list[str]:
    mounts, seen = [], set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2 or not parts[0].startswith("/dev/") or parts[0] in seen:
                    continue
                seen.add(parts[0])
                mounts.append(parts[1].replace("\\040", " "))
    except OSError:
        return ["/"]
    return mounts or ["/"]


def _procs_linux(dt: float) -> list[tuple[int, str, float | None, float]]:
    global _prev_proc
    cur: dict[str, int] = {}
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
        except OSError:
            continue
        rp = data.rfind(")")  # comm kan innehålla ( ) och mellanslag
        if rp < 0:
            continue
        comm = data[data.find("(") + 1:rp]
        fields = data[rp + 2:].split()
        try:
            ticks = int(fields[11]) + int(fields[12])  # utime + stime
            rss_mb = int(fields[21]) * _PAGE / 1e6      # rss i sidor
        except (IndexError, ValueError):
            continue
        cur[pid] = ticks
        cpu_pct = None
        if pid in _prev_proc and dt > 0:
            cpu_pct = round(100.0 * ((ticks - _prev_proc[pid]) / _CLK) / dt, 1)
        out.append((int(pid), comm, cpu_pct, round(rss_mb, 1)))
    _prev_proc = cur
    out.sort(key=lambda s: (s[2] or 0.0, s[3]), reverse=True)
    return out[:PROC_TOPN]


# ---- macOS: best-effort subset ----------------------------------------------

def _mem_macos() -> tuple[float | None, float | None]:
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return (None, None)
    psize = 4096
    m = re.search(r"page size of (\d+)", vm)
    if m:
        psize = int(m.group(1))
    free = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):
        m = re.search(rf"{key}:\s+(\d+)", vm)
        if m:
            free += int(m.group(1)) * psize
    if not total:
        return (None, None)
    return (round(100.0 * (1 - free / total), 1), round(total / 1024 / 1024))


def _procs_macos(_dt: float) -> list[tuple[int, str, float | None, float]]:
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,%cpu=,rss=,comm="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, rss, comm = parts
        try:
            rows.append((int(pid), comm, float(cpu), round(float(rss) / 1024, 1)))  # rss kB -> MB
        except ValueError:
            continue
    rows.sort(key=lambda r: r[2] or 0.0, reverse=True)
    return rows[:PROC_TOPN]


# ---- cross-platform ---------------------------------------------------------

def read_disks() -> list[tuple[str, float, float]]:
    mounts = _mounts_linux() if _SYS == "Linux" else ["/"]
    out = []
    for m in mounts:
        try:
            st = os.statvfs(m)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue
        used_pct = round(100.0 * (1 - st.f_bfree / st.f_blocks), 1)
        free_gb = round(st.f_bavail * st.f_frsize / 1e9, 2)
        out.append((m, used_pct, free_gb))
    return out


def cycle(conn: sqlite3.Connection, dt: float) -> None:
    ts = time.time()
    load1, load5, load15 = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    if _SYS == "Linux":
        cpu_pct = _cpu_linux()
        mem_used, mem_total = _mem_linux()
        temp_c = _temp_linux()
        dr, dw = _diskio_linux(ts)
        procs = _procs_linux(dt)
    else:
        cpu_pct = round(min(100.0, 100.0 * load1 / (os.cpu_count() or 1)), 1) if load1 is not None else None
        mem_used, mem_total = _mem_macos()
        temp_c = None
        dr = dw = None
        procs = _procs_macos(dt)

    conn.execute(
        "INSERT INTO host_samples (ts,cpu_pct,load1,load5,load15,mem_used_pct,mem_total_mb,temp_c,"
        "disk_read_mbps,disk_write_mbps,node) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, cpu_pct, load1, load5, load15, mem_used, mem_total, temp_c, dr, dw, NODE),
    )
    conn.executemany(
        "INSERT INTO disk_samples (ts,mount,used_pct,free_gb,node) VALUES (?,?,?,?,?)",
        [(ts, m, up, fg, NODE) for (m, up, fg) in read_disks()],
    )
    conn.executemany(
        "INSERT INTO proc_samples (ts,pid,name,cpu_pct,rss_mb,node) VALUES (?,?,?,?,?,?)",
        [(ts, pid, name, cpu, rss, NODE) for (pid, name, cpu, rss) in procs],
    )
    conn.commit()


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)
    log(f"start: node={NODE} os={_SYS} interval={INTERVAL}s topn={PROC_TOPN} db={DB_PATH}")
    last = time.time()
    while _running:
        start = time.time()
        try:
            cycle(conn, start - last)
        except Exception as e:  # noqa: BLE001 - en cykel ska aldrig döda daemonen
            log(f"cycle error: {e!r}")
        last = start
        next_t = (int(start // INTERVAL) + 1) * INTERVAL
        sleep = next_t - time.time()
        while sleep > 0 and _running:
            time.sleep(min(sleep, 1.0))
            sleep = next_t - time.time()
    conn.close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
