"""Pipeline / process liveness with a tiny footprint.

Two opt-in signals, both stdlib and bounded:
  * proc-watch: one /proc scan per slow cycle matches configured cmdline substrings
    (e.g. gst-launch-1.0) and reports count, summed cpu%/rss, the youngest process's
    uptime, and a cumulative restart count that increments when the youngest starttime
    changes (crash/flap detection). No `ps`, no log tails.
  * rtsp: one bounded RTSP OPTIONS request per endpoint confirms a camera/stream is
    actually being served (not just that the encoder process exists). One short-lived
    socket, no ffprobe/gst subprocess, no media bytes read.
"""

from __future__ import annotations

import os
import socket
import time

from .. import adapters, config, schema

_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# Cross-cycle state (the collector keeps this module resident between scheduled calls).
_prev_ticks: dict[int, int] = {}     # pid -> cumulative cpu ticks, for cpu% deltas
_prev_ts: float = 0.0
_prev_start: dict[str, int | None] = {}  # label -> youngest starttime seen last cycle
_restarts: dict[str, int] = {}           # label -> cumulative restart count


def _watches(specs: list[str]) -> list[tuple[str, str]]:
    out = []
    for spec in specs:
        if "=" in spec:
            label, pat = (s.strip() for s in spec.split("=", 1))
            if label and pat:
                out.append((label, pat))
    return out


def _rtsp_targets() -> list[tuple[str, str]]:
    out = []
    for spec in config.RTSP_URLS:
        if "=" in spec and "://" in spec.split("=", 1)[1]:
            label, url = (s.strip() for s in spec.split("=", 1))
        else:
            url = spec.strip()
            label = url
        if url:
            out.append((label, url))
    return out


def _btime() -> float:
    """System boot time (epoch seconds) from /proc/stat, for absolute process uptime."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _read_procs() -> list[dict]:
    """All processes with the fields proc-watch needs. Linux /proc only; [] elsewhere."""
    if adapters.SYSTEM != "Linux":
        return []
    out = []
    try:
        it = os.scandir("/proc")
    except OSError:
        return []
    with it:
        for entry in it:
            pid = entry.name
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat") as f:
                    data = f.read()
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").strip().decode("utf-8", "replace")
            except OSError:
                continue
            rp = data.rfind(")")  # comm may contain ( ) and spaces
            if rp < 0:
                continue
            fields = data[rp + 2:].split()
            try:
                ticks = int(fields[11]) + int(fields[12])  # utime + stime
                rss_mb = int(fields[21]) * _PAGE / 1e6
                start_ticks = int(fields[19])              # starttime (clock ticks since boot)
            except (IndexError, ValueError):
                continue
            out.append({"pid": int(pid), "start_ticks": start_ticks, "ticks": ticks,
                        "rss_mb": round(rss_mb, 1), "cmdline": cmd})
    return out


def _rtsp_probe(url: str) -> tuple[int, float | None, str]:
    """Bounded RTSP OPTIONS. Returns (ok, latency_ms, status). status is the RTSP
    'code reason' on success, or the exception class name on failure."""
    host, port, path = _split_rtsp(url)
    start = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=config.RTSP_TIMEOUT)
        try:
            s.settimeout(config.RTSP_TIMEOUT)
            s.sendall(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            line = s.recv(256).split(b"\r\n", 1)[0].decode("utf-8", "replace")
        finally:
            s.close()
    except (OSError, ValueError) as e:
        return 0, None, e.__class__.__name__
    latency_ms = (time.monotonic() - start) * 1000
    parts = line.split(None, 1)  # "RTSP/1.0 200 OK"
    status = parts[1].strip() if len(parts) > 1 else line
    ok = 1 if status.startswith("200") else 0
    return ok, round(latency_ms, 1), status


def _split_rtsp(url: str) -> tuple[str, int, str]:
    rest = url.split("://", 1)[1] if "://" in url else url
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return host or "127.0.0.1", int(port) if port.isdigit() else 554, "/" + path


def _collect_proc_rows(procs: list[dict], ts: float) -> list[dict]:
    global _prev_ts
    dt = ts - _prev_ts if _prev_ts else 0.0
    btime = _btime()
    rows = []
    for label, pat in _watches(config.PROC_WATCH):
        matched = [p for p in procs if pat in p["cmdline"]]
        count = len(matched)
        if matched:
            cpu = None
            if dt > 0:
                delta = sum(p["ticks"] - _prev_ticks[p["pid"]]
                            for p in matched if p["pid"] in _prev_ticks)
                cpu = round(max(0.0, 100.0 * (delta / _CLK) / dt), 1)
            rss = round(sum(p["rss_mb"] for p in matched), 1)
            youngest = max(p["start_ticks"] for p in matched)
            uptime = round(ts - (btime + youngest / _CLK), 1)
        else:
            cpu = rss = uptime = None
            youngest = None
        # Restart detection: the youngest starttime moving forward (or a process
        # reappearing after being gone) means the watched pipeline was (re)started.
        prev = _prev_start.get(label, "unset")
        if prev != "unset" and youngest is not None and (prev is None or youngest > prev):
            _restarts[label] = _restarts.get(label, 0) + 1
        _prev_start[label] = youngest
        rows.append({"ts": ts, "label": label, "count": count, "cpu_pct": cpu,
                     "rss_mb": rss, "uptime_s": uptime, "restarts": _restarts.get(label, 0)})
    return rows


def collect(conn, ts: float | None = None) -> None:
    if not (config.PROC_WATCH or config.RTSP_URLS):
        return
    ts = time.time() if ts is None else ts
    proc_rows = []
    if config.PROC_WATCH:
        procs = _read_procs()
        proc_rows = _collect_proc_rows(procs, ts)
        global _prev_ts
        _prev_ticks.clear()
        _prev_ticks.update({p["pid"]: p["ticks"] for p in procs})
        _prev_ts = ts
    stream_rows = []
    for label, url in _rtsp_targets():
        ok, latency, status = _rtsp_probe(url)
        stream_rows.append({"ts": ts, "url": label, "ok": ok,
                            "latency_ms": latency, "status": status})
    if proc_rows:
        schema.insert(conn, "proc_watch", proc_rows)
    if stream_rows:
        schema.insert(conn, "stream_probes", stream_rows)
    if proc_rows or stream_rows:
        conn.commit()
