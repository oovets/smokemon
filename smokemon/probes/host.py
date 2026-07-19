"""Host health, sampled from /proc + /sys and fed to the detector.

Samples cpu/load/mem/swap/temp/PSI and per-mount disk usage, canonicalises the entity (mount
point for disk signals, empty for the whole-node ones), and hands the values to
incidents.evaluate(). Counter deltas and hardware bitfields that have no continuous baseline
to debounce -- OOM kills, CPU thermal throttles, Pi under-voltage -- go to events instead; see
the comment above those calls for why the split exists.

Nothing is written per cycle. The detector holds samples in memory and persists only what a
rule confirms, so a healthy node's host probe touches no tables at all. What collect() does
leave behind is _LAST, an in-memory cache the heartbeat reads on its own cadence so it never
re-probes /proc.

Tiers (gated by internal timers inside collect() so callers stay simple):
  fast (every cycle):  cpu, load, mem, swap, oom, temp_max, psi, cpu_throttle, mounts,
                       per-zone thermal, own-process footprint
  slow (every 5 min):  vcgencmd get_throttled (Pi)
  vslow (every 60 min):SD-card wear-level (mmcblk* life_time)

The tcp/conntrack and Jetson rail helpers are kept because tests still exercise them and a
future rule could wire them into the detector.

The top-N process scan was deleted outright rather than kept dormant. Its shape belonged to
the old model -- five rows every 30s forever, in the hope someone looks back. The
incident-model version of "what was hogging the CPU" is evidence captured once at trip time,
which is a different feature, not a re-wiring of that function."""

import glob
import os
import re
import resource
import shutil
import subprocess
import time

from .. import events, incidents

_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = resource.getpagesize()
_WHOLE_DISK_RE = re.compile(r"(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+|nvme\d+n\d+)$")
_SLOW_INTERVAL = 300.0     # vcgencmd get_throttled cadence
_VSLOW_INTERVAL = 3600.0   # SD wear-level cadence

_prev_cpu: tuple[int, int] | None = None
_prev_self_cpu: float | None = None
_prev_self_io: tuple[int, float] | None = None  # (summed write_bytes, ts) for the SD-write rate
_last = 0.0
_slow_last = 0.0
_vslow_last = 0.0
_vcgencmd = shutil.which("vcgencmd")  # cached at import: present on Pi, None elsewhere

# Most recent sample, for the heartbeat and the detector to read without re-probing. Empty
# until the first collect() -- callers must treat a missing key as "not measured yet".
_LAST: dict = {}


def last() -> dict:
    return _LAST


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


# ---------- Disk mounts ----------

def _mounts_linux() -> list[str]:
    mounts, seen = [], set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) < 3:
                    continue
                dev, mnt, fstype = p[0], p[1], p[2]
                # squashfs loop mounts -- every snap revision on the box, one per package per
                # version -- are read-only, fixed-size compressed images. They read ~100% used
                # and ~100% inode-used by construction: that is what a full compressed archive
                # looks like, not a capacity signal. Without this filter each one opens its own
                # permanent disk.used_pct/disk.inode_used_pct incident, and a box with 40 snap
                # revisions opens 80 incidents that can never clear.
                if fstype == "squashfs":
                    continue
                if dev.startswith("/dev/") and dev not in seen:
                    seen.add(dev)
                    mounts.append(mnt.replace("\\040", " "))
    except OSError:
        return ["/"]
    return mounts or ["/"]


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


# ---------- Disks (mounts + inode usage) ----------

def _disks() -> list[dict]:
    out = []
    for m in _mounts_linux():
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
    """Current resident set of this daemon (MB), live from /proc/self/statm, falling back
    to ru_maxrss (peak, KiB on Linux)."""
    try:
        with open("/proc/self/statm") as f:
            return round(int(f.read().split()[1]) * _PAGE / 1e6, 1)
    except (OSError, ValueError, IndexError):
        return round(ru.ru_maxrss / 1024, 1)


def _fleet_footprint_linux() -> tuple[float | None, int | None]:
    """This process's RSS and cumulative storage writes from /proc/self.

    The collector now runs the shipper and pruner in-process, so the own-process figure is the
    real multi-daemon footprint; scanning every /proc entry every host cycle was measurable
    work for a metric that should itself be cheap. RSS from statm field 2 (pages); writes from
    io.write_bytes (bytes that actually reached storage - the figure that wears an SD card)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
    except (OSError, ValueError, IndexError):
        return (None, None)
    wbytes = 0
    saw_io = False
    try:  # /proc/self/io is readable for our own uid; absent on some kernels
        with open("/proc/self/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    wbytes = int(line.split()[1])
                    saw_io = True
                    break
    except (OSError, ValueError, IndexError):
        pass
    return (round(pages * _PAGE / 1e6, 1), wbytes if saw_io else None)


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

    write_mb_day = None
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
    if rss_mb is None:  # /proc scan found nothing
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

    info = _meminfo()
    mem_used, mem_total, cache_mb, swap_used = _mem_linux(info)
    zones = _thermal_zones_linux()
    temp_c = round(max(zones.values()), 1) if zones else None  # back-compat with old plot
    cpu_pct = _cpu_linux()
    psi_cpu, psi_mem, psi_io = _psi_linux()
    throttle = _cpu_throttle_linux()
    oom = _oom_count_linux()
    # Slow tier: vcgencmd is ~30ms, only worth probing every few minutes
    pi_bits: int | None = None
    if ts - _slow_last >= _SLOW_INTERVAL:
        _slow_last = ts
        pi_bits = _pi_throttle_bits()

    disks = _disks()
    self_proc = _self_proc(dt)  # S5: always measure our own footprint, not just top-N

    # Very-slow tier: SD-wear shifts by single percent steps, hourly is plenty
    if ts - _vslow_last >= _VSLOW_INTERVAL:
        _vslow_last = ts
        wear = _sd_wear_linux()
        if wear:
            _LAST["wear_pct"] = max((w.get("wear_pct") or 0.0) for w in wear) or None

    # Cache what the heartbeat needs. It runs on its own cadence and must not re-probe: every
    # value here was just read, so reporting it costs nothing, while a second /proc pass would
    # make the observer measurably more expensive than the thing it observes.
    _LAST.update({
        "ts": ts, "cpu_pct": cpu_pct, "load1": load1, "mem_used_pct": mem_used,
        "swap_used_pct": swap_used, "temp_c": temp_c, "psi_cpu": psi_cpu, "psi_mem": psi_mem,
        "psi_io": psi_io, "throttle_bits": pi_bits, "disks": disks,
        "rss_mb": (self_proc or {}).get("rss_mb"),
        "self_cpu_pct": (self_proc or {}).get("cpu_pct"),
        "write_mb_day": (self_proc or {}).get("write_mb_day"),
    })

    # Feed the detector off the values just computed. Disk entity is the mount point, and
    # _mounts_linux() already restricts to real /dev/* filesystems, so tmpfs and overlay
    # noise never becomes a signal with its own baseline.
    incidents.evaluate(conn, "host.temp", "", temp_c, ts)
    incidents.evaluate(conn, "host.mem", "", mem_used, ts)
    incidents.evaluate(conn, "host.swap", "", swap_used, ts)
    incidents.evaluate(conn, "host.psi_cpu", "", psi_cpu, ts)
    incidents.evaluate(conn, "host.psi_io", "", psi_io, ts)
    for d in disks:
        incidents.evaluate(conn, "disk.used_pct", d["mount"], d.get("used_pct"), ts)
        incidents.evaluate(conn, "disk.inode_used_pct", d["mount"], d.get("inode_used_pct"), ts)

    # Discrete/imperative facts only. Thresholds on continuous signals (temperature, swap,
    # memory, PSI) belong to detect.py, which has hysteresis, debounce and a per-node baseline
    # that events.edge does not -- keeping both would mean two detectors for the same condition
    # firing at different thresholds. What stays here is a genuinely different class: counter
    # deltas and hardware bitfields, where the edge IS the event and there is nothing to
    # debounce.
    # uid links these to whatever incident happens to be open right now (best-effort, not
    # causal proof) -- an OOM kill during an open host.mem incident is worth surfacing on that
    # incident's page; one with nothing open ships as unlinked evidence.
    uid = incidents.active_uid(conn)
    events.counter(conn, "host:oom", oom, source="host", severity="crit",
                   event="oom-kill", detail_fn=lambda d: f"{d} new OOM kill(s)", uid=uid)
    events.counter(conn, "host:cpu-throttle", throttle, source="host", severity="warn",
                   event="cpu-throttle", detail_fn=lambda d: f"{d} new CPU thermal-throttle event(s)",
                   uid=uid)
    if pi_bits is not None:  # only on the 5-min Pi sampling tier; bit0 = under-voltage now, bit2 = throttled now
        events.edge(conn, bool(pi_bits & 0x1), "host:undervolt", source="host", severity="crit",
                    event="under-voltage", detail="Pi under-voltage detected", clear_detail="voltage ok",
                    uid=uid)
        events.edge(conn, bool(pi_bits & 0x4), "host:pi-throttle", source="host", severity="warn",
                    event="pi-throttled", detail="Pi currently throttled", clear_detail="not throttled",
                    uid=uid)
