"""Low-footprint Docker container health via the Engine API over its unix socket.

Opt-in (SMOKEMON_DOCKER=1). One short-lived AF_UNIX connection per cycle issuing a
single bounded HTTP/1.0 `GET /containers/json?all=1`; state, health and exit code are
parsed from that one response. No docker CLI subprocess, no `docker logs`, no journal or
log tailing, no event stream. Optional small per-container `inspect` adds restart_count/
oom_killed, and optional cgroup-v2 /sys reads add live cpu/mem. Everything is bounded by
DOCKER_TIMEOUT, DOCKER_MAX_BYTES and DOCKER_MAX.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time

from .. import config, core, schema

_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting|starting)\)")
_EXIT_RE = re.compile(r"Exited \((\d+)\)")
_DAEMON = "__daemon__"  # sentinel row name recorded when the socket is unreachable

# cgroup-v2 cumulative cpu usage per container id: {cid: (usage_usec, ts)} for cpu% deltas.
_prev_cpu: dict[str, tuple[int, float]] = {}

# Daemon reachability is EDGE-triggered: a __daemon__ row is written only when reachability
# changes (up<->down), never every cycle. Without this an unreachable socket would append a down
# row every interval forever - spamming the hub and, worse, making the dashboard's daemon-down
# alert STICKY: services() takes the latest __daemon__ row, so once a down row exists and no up
# row ever follows, a node with healthy containers keeps showing "daemon unreachable" long after
# docker recovered. None = unknown (first cycle). See collect().
_prev_daemon_up: bool | None = None


def _raw_get(path: str) -> bytes:
    """Bounded HTTP/1.0 GET over the docker unix socket; returns the response body.
    HTTP/1.0 means the daemon answers without chunked encoding and closes the socket,
    so we can read to EOF and skip a chunk parser. Raises OSError/ValueError on failure."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(config.DOCKER_TIMEOUT)
    try:
        s.connect(config.DOCKER_SOCK)
        req = (f"GET /{config.DOCKER_API}{path} HTTP/1.0\r\n"
               "Host: docker\r\nAccept: application/json\r\n\r\n")
        s.sendall(req.encode())
        chunks: list[bytes] = []
        total = 0
        while True:
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
            total += len(d)
            if total > config.DOCKER_MAX_BYTES:
                raise ValueError("docker response too large")
        raw = b"".join(chunks)
    finally:
        s.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or not status[1].startswith(b"2"):
        raise ValueError(f"docker http status {status[1:2]!r}")
    return body


def _get_json(path: str):
    return json.loads(_raw_get(path).decode("utf-8", "replace"))


def _health_from_status(status: str | None) -> str:
    m = _HEALTH_RE.search(status or "")
    if not m:
        return ""
    return "starting" if "starting" in m.group(1) else m.group(1)


def _exit_from_status(status: str | None) -> int | None:
    m = _EXIT_RE.search(status or "")
    return int(m.group(1)) if m else None


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _cgroup_sample(cid: str, ts: float) -> dict:
    """Per-container cpu%/mem/pids from cgroup v2 (system.slice/docker-<id>.scope).
    Pure /sys reads. cpu% is the delta of cumulative usage_usec over wall time, in
    percent of a single core (so it can exceed 100 on multi-core, matching proc_samples)."""
    base = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
    mem = _read_int(f"{base}/memory.current")
    pids = _read_int(f"{base}/pids.current")
    usage = None
    try:
        with open(f"{base}/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    usage = int(line.split()[1])
                    break
    except (OSError, ValueError):
        usage = None
    cpu_pct = None
    prev = _prev_cpu.get(cid)
    if usage is not None and prev:
        pu, pt = prev
        dt = ts - pt
        if dt > 0:
            cpu_pct = round(max(0.0, 100.0 * ((usage - pu) / 1e6) / dt), 1)
    if usage is not None:
        _prev_cpu[cid] = (usage, ts)
    return {"cpu_pct": cpu_pct,
            "mem_mb": round(mem / 1e6, 1) if mem is not None else None,
            "pids": pids}


def _inspect(cid: str) -> dict:
    data = _get_json(f"/containers/{cid}/json")
    state = data.get("State") or {}
    health = (state.get("Health") or {}).get("Status") or ""
    return {"restart_count": data.get("RestartCount"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": 1 if state.get("OOMKilled") else 0,
            "health": health}


def collect(conn) -> None:
    global _prev_daemon_up
    if not config.DOCKER_ENABLED:
        return
    # Auto-detect: no socket means no docker on this node -> silent no-op. Only when the
    # probe is explicitly forced on do we record a daemon-down row despite a missing socket.
    if not os.path.exists(config.DOCKER_SOCK) and not config.DOCKER_FORCED:
        return
    ts = time.time()
    try:
        containers = _get_json("/containers/json?all=1")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # Edge-triggered: record the down row + log only on the up->down transition (or the very
        # first cycle), not every interval, so an unreachable socket can't spam down rows.
        if _prev_daemon_up is not False:
            core.log(f"docker probe failed: {e.__class__.__name__}")
            schema.insert(conn, "docker_samples", [{"ts": ts, "name": _DAEMON, "running": 0}])
            conn.commit()
        _prev_daemon_up = False
        return

    rows = []
    # Write one __daemon__ up row on the down->up transition AND on the first cycle after start
    # (prev is None): a process restart loses the in-memory state, and the hub may still hold a
    # stale down sentinel from before — emitting an up row on first success clears it. Steady-state
    # success (prev already True) writes no daemon row at all.
    if _prev_daemon_up is not True:
        rows.append({"ts": ts, "name": _DAEMON, "running": 1})
    _prev_daemon_up = True
    live_cids: set[str] = set()
    for c in containers[:config.DOCKER_MAX]:
        cid = c.get("Id") or ""
        if cid:
            live_cids.add(cid)
        names = c.get("Names") or ["/?"]
        state = c.get("State")
        status = c.get("Status") or ""
        running = 1 if state == "running" else 0
        row = {"ts": ts, "name": names[0].lstrip("/"), "image": c.get("Image"),
               "state": state, "running": running,
               "health": _health_from_status(status),
               "exit_code": _exit_from_status(status),
               "restart_count": None, "oom_killed": None,
               "cpu_pct": None, "mem_mb": None, "pids": None}
        if config.DOCKER_INSPECT and cid:
            try:
                ins = _inspect(cid)
                row["restart_count"] = ins["restart_count"]
                if ins["exit_code"] is not None:
                    row["exit_code"] = ins["exit_code"]
                row["oom_killed"] = ins["oom_killed"]
                if ins["health"]:
                    row["health"] = ins["health"]
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if config.DOCKER_CGROUP and running and cid:
            row.update(_cgroup_sample(cid, ts))
        rows.append(row)
    # Drop cpu-delta state for containers that no longer exist so _prev_cpu can't grow
    # without bound on a node that churns through many short-lived containers.
    for stale in [c for c in _prev_cpu if c not in live_cids]:
        del _prev_cpu[stale]
    if rows:
        schema.insert(conn, "docker_samples", rows)
        conn.commit()
