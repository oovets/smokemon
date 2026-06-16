"""Active TCP liveness checks (opt-in via SMOKEMON_TCP_CHECK).

For each configured name=host:port the node opens one short-lived connection and reads at least
MIN_BYTES within the timeout. Reading bytes (not just connecting) is the point: a stalled feed - a
video stream that keeps its TCP socket open but stops sending frames - still trips the check,
where a plain connect() would pass. The hub pages a down check as a sev3 'tcpcheck' alert. Pure
stdlib socket, no payload sent (we only read), so it is safe against arbitrary line protocols.
"""

from __future__ import annotations

import socket
import time

from .. import config, core, schema


def _parse(spec: str):
    """'name=host:port' or 'name=host:port:minbytes' -> (name, host, port, minbytes|None)."""
    name, sep, target = spec.partition("=")
    if not sep:
        return None
    parts = target.strip().rsplit(":", 2)
    if len(parts) == 3:
        host, port, mb = parts
    elif len(parts) == 2:
        host, port, mb = parts[0], parts[1], None
    else:
        return None
    try:
        return name.strip(), host, int(port), (int(mb) if mb is not None else None)
    except ValueError:
        return None


def _check(host: str, port: int, min_bytes: int, timeout: float):
    """(ok, latency_ms, nbytes, detail). ok only if connected AND >= min_bytes arrived in time."""
    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return False, None, 0, f"connect failed ({e.__class__.__name__})"
    try:
        sock.settimeout(timeout)
        got = 0
        while got < min_bytes:
            chunk = sock.recv(4096)
            if not chunk:                      # peer closed before sending enough data
                break
            got += len(chunk)
        latency = (time.time() - t0) * 1000.0
        if got >= min_bytes:
            return True, latency, got, "ok"
        return False, latency, got, f"no data ({got}/{min_bytes} bytes)"
    except socket.timeout:
        return False, (time.time() - t0) * 1000.0, 0, "read timeout (socket open, no data)"
    except OSError as e:
        return False, (time.time() - t0) * 1000.0, 0, f"read failed ({e.__class__.__name__})"
    finally:
        sock.close()


def collect(conn) -> None:
    if not config.TCP_CHECK:
        return
    ts = time.time()
    timeout = config.TCP_CHECK_TIMEOUT
    rows = []
    for spec in config.TCP_CHECK:
        parsed = _parse(spec)
        if not parsed:
            core.log(f"tcpcheck: bad SMOKEMON_TCP_CHECK entry {spec!r}")
            continue
        name, host, port, mb = parsed
        min_bytes = mb if mb is not None else config.TCP_CHECK_MIN_BYTES
        ok, latency, nbytes, detail = _check(host, port, min_bytes, timeout)
        # explicit opt-in check: always record (down included) so a feed that is already down
        # surfaces immediately, unlike the auto-detected redis/docker probes.
        rows.append({"ts": ts, "name": name, "host": host, "port": port,
                     "ok": 1 if ok else 0, "latency_ms": latency, "bytes": nbytes, "detail": detail})
    if rows:
        schema.insert(conn, "tcp_checks", rows)
        conn.commit()
