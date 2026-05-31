"""Host health: cpu/load/mem/temp/disk + disk IO + top-N procs + PSI + thermal zones +
power rails + tcp/conntrack + cpu freq/throttle + SD-wear + Pi under-voltage history.

Tiers (all gated by an internal counter inside collect() so callers stay simple):
  fast (every cycle):  cpu, load, mem, swap, cache, oom, temp_max, psi, cpu_freq,
                       cpu_throttle, disk IO, mounts, per-zone thermal, tcp/conntrack,
                       per-rail power (Jetson), top-N procs
  slow (every 5 min):  vcgencmd get_throttled (Pi)
  vslow (every 60 min):SD-card wear-level (mmcblk* life_time)

Linux: full via /proc + /sys (incl. Jetson INA3221 + thermal). macOS: subset."""

import glob
import os
import re
import resource
import shutil
import subprocess
import time

from .. import adapters, config, schema

_SYS = adapters.SYSTEM
_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = resource.getpagesize()
_WHOLE_DISK_RE = re.compile(r"(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+|nvme\d+n\d+)$")
_SLOW_INTERVAL = 300.0     # vcgencmd get_throttled cadence
_VSLOW_INTERVAL = 3600.0   # SD wear-level cadence

_prev_cpu: tuple[int, int] | None = None
_prev_proc: dict[str, int] = {}
_prev_self_cpu: float | None = None
_prev_self_io: tuple[int, float] | None = None  # (summed write_bytes, ts) for the SD-write rate
_prev_diskio: tuple[int, int, float] | None = None
_last = 0.0
_slow_last = 0.0
_vslow_last = 0.0
_vcgencmd = shutil.which("vcgencmd")  # cached at import: present on Pi, None elsewhere


# ---------- CPU / load ----------

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


def _cpu_freq_linux() -> float | None:
    """Average current frequency across all cores, in MHz. Detects throttling that
    cpu_pct cannot see ('100% busy at 600 MHz' looks the same as 'at 1500 MHz')."""
    freqs = []
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            with open(p) as f:
                freqs.append(int(f.read().strip()) / 1000.0)  # kHz -> MHz
        except (OSError, ValueError, TypeError):
            continue
    return round(sum(freqs) / len(freqs), 1) if freqs else None


def _cpu_throttle_linux() -> int | None:
    """Sum of per-core thermal_throttle counters (x86 only; ARM has no such counter)."""
    total = 0
    found = False
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/core_throttle_count"):
        try:
            with open(p) as f:
                total += int(f.read().strip())
                found = True
        except (OSError, ValueError, TypeError):
            continue
    return total if found else None


# ---------- Memory / swap / OOM ----------

def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.strip().split()[0])  # kB
    except (OSError, ValueError, IndexError):
        return {}
    return info


def _mem_linux(info: dict[str, int]) -> tuple[float | None, float | None, float | None, float | None]:
    """(used_pct, total_mb, cache_mb, swap_used_pct)"""
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    cache_kb = info.get("Cached", 0) + info.get("Buffers", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    used_pct = round(100.0 * (1 - avail / total), 1) if total else None
    total_mb = round(total / 1024) if total else None
    cache_mb = round(cache_kb / 1024, 1) if cache_kb else 0.0
    swap_used_pct = round(100.0 * (1 - swap_free / swap_total), 1) if swap_total else 0.0
    return (used_pct, total_mb, cache_mb, swap_used_pct)


def _oom_count_linux() -> int | None:
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith("oom_kill "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


# ---------- PSI (pressure stall information; Linux >= 4.20) ----------

def _psi_one(path: str) -> float | None:
    """First line of /proc/pressure/* looks like:
       some avg10=0.00 avg60=0.00 avg300=0.00 total=0
       We read the 10-second rolling average ('some')."""
    try:
        with open(path) as f:
            line = f.readline()
        m = re.search(r"avg10=([0-9.]+)", line)
    except (OSError, TypeError):  # quirky read may return None mid-decode
        return None
    return float(m.group(1)) if m else None


def _psi_linux() -> tuple[float | None, float | None, float | None]:
    return (_psi_one("/proc/pressure/cpu"),
            _psi_one("/proc/pressure/memory"),
            _psi_one("/proc/pressure/io"))


# ---------- Thermal (all zones, not just max) ----------

def _thermal_zones_linux() -> dict[str, float]:
    """{zone_name: temp_c} for every readable /sys/class/thermal/thermal_zone*."""
    out: dict[str, float] = {}
    for tpath in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        zdir = os.path.dirname(tpath)
        try:
            with open(tpath) as f:
                temp = int(f.read().strip()) / 1000.0
        except (OSError, ValueError, TypeError):  # quirky sensors return None mid-read (EAGAIN)
            continue
        zname = None
        try:
            with open(os.path.join(zdir, "type")) as f:
                zname = f.read().strip()
        except OSError:
            zname = os.path.basename(zdir)
        out[zname or os.path.basename(zdir)] = round(temp, 1)
    return out


# ---------- Disk IO + mounts ----------

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


# ---------- Processes ----------

def _procs_linux(dt: float) -> list[dict]:
    global _prev_proc
    cur: dict[str, int] = {}
    out = []
    # scandir avoids stat()ing every /proc entry and skips non-pid kernel directories
    # without a Python-side isdigit() check on every name.
    with os.scandir("/proc") as it:
        for entry in it:
            pid = entry.name
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


# ---------- TCP / UDP / conntrack ----------

def _snmp_section(lines: list[str], name: str) -> dict[str, int] | None:
    """/proc/net/snmp has each protocol on two lines: a header + values, both starting
    with 'Tcp:' (or 'Udp:'). Returns {header_col: int_value}, or None if absent."""
    header = values = None
    for line in lines:
        if line.startswith(name + ":"):
            if header is None:
                header = line.split()[1:]
            else:
                values = line.split()[1:]
                break
    if not header or not values or len(header) != len(values):
        return None
    out: dict[str, int] = {}
    for k, v in zip(header, values):
        try:
            out[k] = int(v)
        except ValueError:
            continue
    return out


def _tcp_metrics_linux() -> dict[str, int | None]:
    try:
        with open("/proc/net/snmp") as f:
            lines = f.readlines()
    except OSError:
        return {}
    tcp = _snmp_section(lines, "Tcp") or {}
    udp = _snmp_section(lines, "Udp") or {}

    def _read_int(p: str) -> int | None:
        try:
            with open(p) as f:
                return int(f.read().strip())
        except (OSError, ValueError, TypeError):
            return None

    return {
        "retrans_segs": tcp.get("RetransSegs"),
        "out_rsts": tcp.get("OutRsts"),
        "estab_resets": tcp.get("EstabResets"),
        "udp_in_errors": udp.get("InErrors"),
        "udp_no_ports": udp.get("NoPorts"),
        "conntrack_used": _read_int("/proc/sys/net/netfilter/nf_conntrack_count"),
        "conntrack_max": _read_int("/proc/sys/net/netfilter/nf_conntrack_max"),
    }


# ---------- Jetson per-rail power (INA3221) ----------

def _jetson_power_linux() -> list[dict]:
    """Read /sys/bus/i2c/drivers/ina3221*/.../iio:device* tree. Layout varies between
    JetPack versions; we probe the well-known files and group by channel index."""
    rails: list[dict] = []
    for dev in glob.glob("/sys/bus/i2c/drivers/ina3221*/*/iio:device*"):
        # Files we may find: in_current{N}_input (mA), in_voltage{N}_input (mV),
        # in_power{N}_input (mW), rail_name_{N}. N is typically 0/1/2 per chip.
        channels: dict[str, dict[str, float | str | None]] = {}
        try:
            entries = os.listdir(dev)
        except OSError:
            continue  # sysfs entry vanished between glob and listdir (unbind/suspend)
        for f in entries:
            m = re.match(r"in_(current|voltage|power)(\d+)_input$", f)
            if m:
                kind, idx = m.group(1), m.group(2)
                try:
                    with open(os.path.join(dev, f)) as fh:
                        channels.setdefault(idx, {})[kind] = float(fh.read().strip())
                except (OSError, ValueError):
                    continue
                continue
            m = re.match(r"(?:rail_name|in_)?label[_ ]?(\d+)?$", f)  # name files vary by JetPack
            if m and m.group(1) is not None:
                try:
                    with open(os.path.join(dev, f)) as fh:
                        channels.setdefault(m.group(1), {})["name"] = fh.read().strip()
                except OSError:
                    continue
        for idx, ch in channels.items():
            amps = ch["current"] / 1000.0 if ch.get("current") is not None else None  # mA -> A
            volts = ch["voltage"] / 1000.0 if ch.get("voltage") is not None else None  # mV -> V
            if "power" in ch and ch["power"] is not None:
                watts = float(ch["power"]) / 1000.0  # mW -> W
            elif amps is not None and volts is not None:
                watts = round(amps * volts, 3)
            else:
                watts = None
            rail = ch.get("name") or f"ina_{idx}"
            rails.append({"rail": str(rail), "watts": watts,
                          "volts": round(volts, 3) if volts is not None else None,
                          "amps": round(amps, 3) if amps is not None else None})
    return rails


# ---------- Jetson GPU util/frequency (sysfs only) ----------

def _read_float(path: str, scale: float = 1.0) -> float | None:
    try:
        with open(path) as f:
            return float(f.read().strip()) / scale
    except (OSError, ValueError, TypeError):
        return None


def _jetson_gpu_linux() -> list[dict]:
    """Read GPU busy/frequency from sysfs/devfreq. No tegrastats/nvidia-smi process.
    JetPack paths vary, so this best-effort probe accepts the common actmon/devfreq
    names and returns an empty list when unavailable."""
    out = []
    seen = set()
    paths = set(glob.glob("/sys/devices/*gpu*/devfreq/*") + glob.glob("/sys/class/devfreq/*gpu*"))
    for dev in sorted(paths):
        real = os.path.realpath(dev)
        if real in seen:
            continue
        seen.add(real)
        name = os.path.basename(dev)
        util = None
        for candidate in ("load", "device/load", "busy"):
            util = _read_float(os.path.join(dev, candidate))
            if util is not None:
                break
        if util is not None and util > 100.0:
            util = util / 10.0 if util <= 1000.0 else util / 1000.0
        freq = _read_float(os.path.join(dev, "cur_freq"), 1_000_000.0)
        if freq is None:
            freq = _read_float(os.path.join(dev, "device/cur_freq"), 1_000_000.0)
        if util is not None or freq is not None:
            out.append({"gpu": name, "util_pct": round(util, 1) if util is not None else None,
                        "freq_mhz": round(freq, 1) if freq is not None else None})
    return out


# ---------- Pi vcgencmd get_throttled (slow tier) ----------

def _pi_throttle_bits() -> int | None:
    """Returns the raw 32-bit field from `vcgencmd get_throttled`, e.g. 0x50005.
       Bit 0 = under-voltage now, 1 = arm freq capped, 2 = throttled,
       16-19 = same conditions sticky since boot. None on non-Pi or on error."""
    if not _vcgencmd:
        return None
    try:
        out = subprocess.run([_vcgencmd, "get_throttled"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


# ---------- SD-card wear-level (very-slow tier) ----------

def _sd_wear_linux() -> list[dict]:
    """eMMC/SD wear from mmcblk*/device/life_time: two hex values separated by space.
    Each step represents 10% of estimated lifetime used (0x01 = 0-10%, 0x0A = 90-100%).
    We report the max of the two as a conservative percent estimate."""
    out = []
    for life in glob.glob("/sys/block/mmcblk*/device/life_time"):
        device = life.split("/")[3]  # mmcblk0
        try:
            with open(life) as f:
                parts = f.read().strip().split()
            vals = [int(p, 16) for p in parts]
            wear_pct = round(max(vals) * 10.0, 1) if vals else None
        except (OSError, ValueError):
            wear_pct = None
        ioerr = None
        try:
            with open(f"/sys/block/{device}/device/ioerr_cnt") as f:
                ioerr = int(f.read().strip())
        except (OSError, ValueError):
            pass
        if wear_pct is not None or ioerr is not None:
            out.append({"device": device, "wear_pct": wear_pct, "ioerr_count": ioerr})
    return out


# ---------- macOS subset ----------

def _sh(args: list[str], timeout: float = 5.0) -> str | None:
    """Run a command, return stdout text, or None on any failure. Used for the
    macOS subprocess helpers below where every individual probe is best-effort."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return None


def _mem_macos() -> tuple[float | None, float | None, float | None]:
    """(used_pct, total_mb, cache_mb). vm_stat reports pages; classify file-backed
    pages as 'cache' so the host panel can distinguish app pressure from disk cache."""
    total_s = _sh(["sysctl", "-n", "hw.memsize"])
    vm = _sh(["vm_stat"])
    if not total_s or not vm:
        return (None, None, None)
    try:
        total = int(total_s.strip())
    except ValueError:
        return (None, None, None)
    m = re.search(r"page size of (\d+)", vm)
    psize = int(m.group(1)) if m else 4096
    free = sum(int(mm.group(1)) * psize for key in ("Pages free", "Pages inactive", "Pages speculative")
               if (mm := re.search(rf"{key}:\s+(\d+)", vm)))
    file_backed = sum(int(mm.group(1)) * psize for key in ("File-backed pages",)
                      if (mm := re.search(rf"{key}:\s+(\d+)", vm)))
    used_pct = round(100.0 * (1 - free / total), 1) if total else None
    total_mb = round(total / 1024 / 1024) if total else None
    cache_mb = round(file_backed / 1e6, 1) if file_backed else None
    return (used_pct, total_mb, cache_mb)


def _swap_macos() -> float | None:
    """swap_used_pct from `sysctl vm.swapusage`: total = N.NNM  used = N.NNM  ..."""
    out = _sh(["sysctl", "-n", "vm.swapusage"])
    if not out:
        return None
    tot_m = re.search(r"total\s*=\s*([\d.]+)M", out)
    use_m = re.search(r"used\s*=\s*([\d.]+)M", out)
    if not tot_m or not use_m:
        return None
    try:
        total = float(tot_m.group(1)); used = float(use_m.group(1))
    except ValueError:
        return None
    return round(100.0 * used / total, 1) if total else 0.0


def _thermal_macos() -> dict[str, float]:
    """macOS does not expose CPU temperatures without sudo+powermetrics. What we
    CAN read is the thermal speed-limit from `pmset -g therm`: when the system
    throttles due to heat, CPU_Speed_Limit drops below 100. We report it as a
    pseudo-zone 'cpu_speed_limit_pct' so the thermal panel always has something
    on macOS - 100.0 means 'no throttling', less means throttled."""
    out = _sh(["pmset", "-g", "therm"])
    if out is None:
        return {}
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    return {"cpu_speed_limit_pct": float(m.group(1)) if m else 100.0}


def _power_macos() -> list[dict]:
    """One 'battery' rail on laptops, empty on desktops. Watts = |V * I|.
    On AC at full charge Amperage is 0, so the rail reports 0 W (a true reading)."""
    out = _sh(["ioreg", "-rc", "AppleSmartBattery"])
    if not out or "AppleSmartBattery" not in out:
        return []
    fields = {}
    for key in ("Voltage", "Amperage", "InstantAmperage"):
        m = re.search(rf'"{key}"\s*=\s*(-?\d+)', out)
        if m:
            fields[key] = int(m.group(1))
    if "Voltage" not in fields:
        return []
    volts = fields["Voltage"] / 1000.0  # mV -> V
    amps_mA = fields.get("InstantAmperage", fields.get("Amperage", 0))
    amps = abs(amps_mA) / 1000.0  # mA -> A; abs() because discharge is negative
    return [{"rail": "battery", "watts": round(volts * amps, 3),
             "volts": round(volts, 3), "amps": round(amps, 3)}]


def _tcp_metrics_macos() -> dict[str, int | None]:
    """Parse `netstat -s -p tcp` + `-p udp`. Conntrack is None (pf state count
    requires root via `pfctl -si`, which we don't want to demand)."""
    tcp_out = _sh(["netstat", "-s", "-p", "tcp"], timeout=10) or ""
    udp_out = _sh(["netstat", "-s", "-p", "udp"], timeout=10) or ""

    def _grab(text: str, pattern: str) -> int | None:
        m = re.search(pattern, text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    # macOS netstat pluralizes nouns for counts != 1 (BSD plural() macro), so the
    # patterns must accept both "data packet (0 byte)" and "data packets (N bytes)".
    return {
        "retrans_segs": _grab(tcp_out, r"(\d+)\s+data packets?\s*\(\d+\s+bytes?\)\s+retransmitted"),
        "out_rsts": _grab(tcp_out, r"(\d+)\s+bad resets?"),
        "estab_resets": _grab(tcp_out, r"(\d+)\s+connections?\s+dropped by rexmit timeout"),
        "udp_in_errors": _grab(udp_out, r"(\d+)\s+with bad checksum"),
        "udp_no_ports": _grab(udp_out, r"(\d+)\s+dropped due to no socket"),
        "conntrack_used": None,
        "conntrack_max": None,
    }


def _procs_macos() -> list[dict]:
    out = _sh(["ps", "-Ao", "pid=,%cpu=,rss=,comm="], timeout=10)
    if not out:
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


# ---------- Disks (mounts + inode usage) ----------

def _disks() -> list[dict]:
    out = []
    for m in (_mounts_linux() if _SYS == "Linux" else ["/"]):
        try:
            st = os.statvfs(m)
        except OSError:
            continue
        if not st.f_blocks:
            continue
        used_pct = round(100.0 * (1 - st.f_bfree / st.f_blocks), 1)
        free_gb = round(st.f_bavail * st.f_frsize / 1e9, 2)
        inode_pct = round(100.0 * (1 - st.f_ffree / st.f_files), 1) if st.f_files else None
        out.append({"mount": m, "used_pct": used_pct, "free_gb": free_gb, "inode_used_pct": inode_pct})
    return out


# ---------- S5: self-instrumentation ----------

def _self_rss_mb(ru) -> float | None:
    """Current resident set of this daemon (MB). Linux exposes the live value in
    /proc/self/statm; elsewhere fall back to ru_maxrss (peak), whose unit differs by
    platform (Linux=KiB, macOS/BSD=bytes)."""
    if _SYS == "Linux":
        try:
            with open("/proc/self/statm") as f:
                return round(int(f.read().split()[1]) * _PAGE / 1e6, 1)
        except (OSError, ValueError, IndexError):
            return round(ru.ru_maxrss / 1024, 1)
    return round(ru.ru_maxrss / 1e6, 1)


def _fleet_footprint_linux() -> tuple[float | None, int | None]:
    """One /proc pass summing RSS and lifetime storage writes across *every* smokemon
    process (collect fast + slow, transient shipper/iperf), so the self panel reports the
    honest multi-daemon footprint - README's "~30 MB" is per process, not the real
    steady-state - and exposes SD write load. RSS from statm field 2 (pages); writes from
    io.write_bytes (bytes that actually reached storage, the figure that wears an SD card).
    A process is "smokemon" if its cmdline mentions the package."""
    pages = wbytes = 0
    saw_rss = saw_io = False
    try:
        scan = os.scandir("/proc")
    except OSError:
        return (None, None)
    with scan as it:
        for entry in it:
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                    if b"smokemon" not in f.read():
                        continue
            except OSError:
                continue
            try:
                with open(f"/proc/{entry.name}/statm") as f:
                    pages += int(f.read().split()[1])
                    saw_rss = True
            except (OSError, ValueError, IndexError):
                pass
            try:  # /proc/<pid>/io is readable for our own uid; absent on some kernels
                with open(f"/proc/{entry.name}/io") as f:
                    for line in f:
                        if line.startswith("write_bytes:"):
                            wbytes += int(line.split()[1])
                            saw_io = True
                            break
            except (OSError, ValueError, IndexError):
                pass
    return (round(pages * _PAGE / 1e6, 1) if saw_rss else None, wbytes if saw_io else None)


def _self_proc(dt: float) -> dict | None:
    """smokemon's own footprint as a proc_samples row named 'smokemon'. The top-N
    sampler would usually miss it (low cpu), so we record it explicitly - this is what
    backs the `self` panel. rss_mb is summed over all smokemon pids on Linux (an honest
    multi-daemon number); cpu% is this process's delta of cumulative user+system CPU over
    dt; write_mb_day projects the fleet's recent SD-write rate so wear is as visible as RSS.
    Read-only of /proc, stdlib."""
    global _prev_self_cpu, _prev_self_io
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF)
    except (OSError, ValueError):
        return None
    cpu_secs = ru.ru_utime + ru.ru_stime
    cpu_pct = None
    if _prev_self_cpu is not None and dt > 0:
        cpu_pct = round(max(0.0, 100.0 * (cpu_secs - _prev_self_cpu) / dt), 1)
    _prev_self_cpu = cpu_secs

    rss_mb = write_mb_day = None
    if _SYS == "Linux":
        rss_mb, wbytes = _fleet_footprint_linux()
        if wbytes is not None:
            now = time.time()
            if _prev_self_io is not None:
                pb, pt = _prev_self_io
                span = now - pt
                # Ignore drops: a restarted pid resets its counter, which would read negative.
                if span > 0 and wbytes >= pb:
                    write_mb_day = round((wbytes - pb) / 1e6 / span * 86400.0, 1)
            _prev_self_io = (wbytes, now)
    if rss_mb is None:  # non-Linux, or /proc scan found nothing
        rss_mb = _self_rss_mb(ru)
    return {"pid": os.getpid(), "name": "smokemon", "cpu_pct": cpu_pct,
            "rss_mb": rss_mb, "write_mb_day": write_mb_day}


# ---------- Main collect() ----------

def collect(conn) -> None:
    global _last, _slow_last, _vslow_last
    ts = time.time()
    dt = ts - _last if _last else 0.0
    _last = ts
    load1, load5, load15 = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)

    if _SYS == "Linux":
        info = _meminfo()
        mem_used, mem_total, cache_mb, swap_used = _mem_linux(info)
        zones = _thermal_zones_linux()
        temp_c = round(max(zones.values()), 1) if zones else None  # back-compat with old plot
        cpu_pct = _cpu_linux()
        dr, dw = _diskio_linux(ts)
        psi_cpu, psi_mem, psi_io = _psi_linux()
        cpu_freq = _cpu_freq_linux()
        throttle = _cpu_throttle_linux()
        oom = _oom_count_linux()
        tcp = _tcp_metrics_linux()
        rails = _jetson_power_linux()
        gpus = _jetson_gpu_linux()
        procs = _procs_linux(dt)
    else:
        cpu_pct = round(min(100.0, 100.0 * load1 / (os.cpu_count() or 1)), 1) if load1 is not None else None
        mem_used, mem_total, cache_mb = _mem_macos()
        swap_used = _swap_macos()
        zones = _thermal_macos()
        # temp_c stays None on macOS since the speed-limit zone is a percentage,
        # not a temperature -- the host panel's "temp NNC" overlay would be misleading.
        temp_c = None
        dr = dw = None
        # PSI has no macOS equivalent without sudo+powermetrics; freq is locked on
        # Apple Silicon. Both remain Linux-only and stay NULL in host_samples.
        psi_cpu = psi_mem = psi_io = cpu_freq = throttle = oom = None
        tcp = _tcp_metrics_macos()
        rails = _power_macos()
        gpus = []
        procs = _procs_macos()

    # Slow tier: vcgencmd is ~30ms, only worth probing every few minutes
    pi_bits: int | None = None
    if ts - _slow_last >= _SLOW_INTERVAL:
        _slow_last = ts
        pi_bits = _pi_throttle_bits() if _SYS == "Linux" else None

    schema.insert(conn, "host_samples", [{
        "ts": ts, "cpu_pct": cpu_pct, "load1": load1, "load5": load5, "load15": load15,
        "mem_used_pct": mem_used, "mem_total_mb": mem_total, "temp_c": temp_c,
        "disk_read_mbps": dr, "disk_write_mbps": dw,
        "swap_used_pct": swap_used, "cache_mb": cache_mb, "oom_kill_count": oom,
        "psi_cpu": psi_cpu, "psi_mem": psi_mem, "psi_io": psi_io,
        "cpu_freq_mhz": cpu_freq, "cpu_throttle_count": throttle, "pi_throttle_bits": pi_bits,
    }])
    schema.insert(conn, "disk_samples", [{"ts": ts, **d} for d in _disks()])
    self_proc = _self_proc(dt)  # S5: always record our own footprint, not just top-N
    schema.insert(conn, "proc_samples", [{"ts": ts, **p} for p in (procs + ([self_proc] if self_proc else []))])
    if zones:
        schema.insert(conn, "thermal_zones", [{"ts": ts, "zone": z, "temp_c": t} for z, t in zones.items()])
    if rails:
        schema.insert(conn, "power_samples", [{"ts": ts, **r} for r in rails])
    if gpus:
        schema.insert(conn, "gpu_samples", [{"ts": ts, **g} for g in gpus])
    if tcp:
        schema.insert(conn, "tcp_samples", [{"ts": ts, **tcp}])

    # Very-slow tier: SD-wear shifts by single percent steps, hourly is plenty
    if _SYS == "Linux" and ts - _vslow_last >= _VSLOW_INTERVAL:
        _vslow_last = ts
        wear = _sd_wear_linux()
        if wear:
            schema.insert(conn, "disk_health", [{"ts": ts, **w} for w in wear])

    conn.commit()
