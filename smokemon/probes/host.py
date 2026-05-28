"""Host health: cpu/load/mem/temp/disk + disk IO + top-N procs.
Linux: full via /proc + /sys (incl. Jetson thermal). macOS: subset (load/mem/disk/procs)."""

import glob
import os
import re
import resource
import subprocess
import time

from .. import adapters, config, schema

_SYS = adapters.SYSTEM
_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = resource.getpagesize()
_WHOLE_DISK_RE = re.compile(r"(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+|nvme\d+n\d+)$")

_prev_cpu: tuple[int, int] | None = None
_prev_proc: dict[str, int] = {}
_prev_diskio: tuple[int, int, float] | None = None
_last = 0.0


def _cpu_linux() -> float | None:
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    pct = None
    if _prev_cpu:
        dtotal, didle = total - _prev_cpu[0], idle - _prev_cpu[1]
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
    return (round(100.0 * (1 - avail / total), 1), round(total / 1024)) if total else (None, None)


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
    global _prev_diskio
    rb = wb = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) >= 14 and _WHOLE_DISK_RE.match(p[2]):
                    rb += int(p[5]) * 512
                    wb += int(p[9]) * 512
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
                p = line.split()
                if len(p) >= 2 and p[0].startswith("/dev/") and p[0] not in seen:
                    seen.add(p[0])
                    mounts.append(p[1].replace("\\040", " "))
    except OSError:
        return ["/"]
    return mounts or ["/"]


def _procs_linux(dt: float) -> list[dict]:
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
        rp = data.rfind(")")  # comm may contain ( ) and spaces
        if rp < 0:
            continue
        comm = data[data.find("(") + 1:rp]
        fields = data[rp + 2:].split()
        try:
            ticks = int(fields[11]) + int(fields[12])
            rss_mb = int(fields[21]) * _PAGE / 1e6
        except (IndexError, ValueError):
            continue
        cur[pid] = ticks
        cpu = round(100.0 * ((ticks - _prev_proc[pid]) / _CLK) / dt, 1) if pid in _prev_proc and dt > 0 else None
        out.append({"pid": int(pid), "name": comm, "cpu_pct": cpu, "rss_mb": round(rss_mb, 1)})
    _prev_proc = cur
    out.sort(key=lambda s: (s["cpu_pct"] or 0.0, s["rss_mb"]), reverse=True)
    return out[:config.PROC_TOPN]


def _mem_macos() -> tuple[float | None, float | None]:
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return (None, None)
    m = re.search(r"page size of (\d+)", vm)
    psize = int(m.group(1)) if m else 4096
    free = sum(int(mm.group(1)) * psize for key in ("Pages free", "Pages inactive", "Pages speculative")
               if (mm := re.search(rf"{key}:\s+(\d+)", vm)))
    return (round(100.0 * (1 - free / total), 1), round(total / 1024 / 1024)) if total else (None, None)


def _procs_macos() -> list[dict]:
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,%cpu=,rss=,comm="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append({"pid": int(parts[0]), "name": parts[3], "cpu_pct": float(parts[1]),
                         "rss_mb": round(float(parts[2]) / 1024, 1)})
        except ValueError:
            continue
    rows.sort(key=lambda r: r["cpu_pct"] or 0.0, reverse=True)
    return rows[:config.PROC_TOPN]


def _disks() -> list[dict]:
    out = []
    for m in (_mounts_linux() if _SYS == "Linux" else ["/"]):
        try:
            st = os.statvfs(m)
        except OSError:
            continue
        if st.f_blocks:
            out.append({"mount": m, "used_pct": round(100.0 * (1 - st.f_bfree / st.f_blocks), 1),
                        "free_gb": round(st.f_bavail * st.f_frsize / 1e9, 2)})
    return out


def collect(conn) -> None:
    global _last
    ts = time.time()
    dt = ts - _last if _last else 0.0
    _last = ts
    load1, load5, load15 = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    if _SYS == "Linux":
        cpu_pct, (mem_used, mem_total), temp_c = _cpu_linux(), _mem_linux(), _temp_linux()
        dr, dw = _diskio_linux(ts)
        procs = _procs_linux(dt)
    else:
        cpu_pct = round(min(100.0, 100.0 * load1 / (os.cpu_count() or 1)), 1) if load1 is not None else None
        mem_used, mem_total = _mem_macos()
        temp_c = dr = dw = None
        procs = _procs_macos()
    schema.insert(conn, "host_samples", [{
        "ts": ts, "cpu_pct": cpu_pct, "load1": load1, "load5": load5, "load15": load15,
        "mem_used_pct": mem_used, "mem_total_mb": mem_total, "temp_c": temp_c,
        "disk_read_mbps": dr, "disk_write_mbps": dw}])
    schema.insert(conn, "disk_samples", [{"ts": ts, **d} for d in _disks()])
    schema.insert(conn, "proc_samples", [{"ts": ts, **p} for p in procs])
    conn.commit()
