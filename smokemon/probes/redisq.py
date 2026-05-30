"""Low-footprint Redis stream/queue health via the Redis RESP protocol.

This probe is opt-in (SMOKEMON_REDIS=1). It uses one short-lived TCP connection,
bounded by SMOKEMON_REDIS_TIMEOUT, and issues only tiny commands: PING, INFO memory,
XLEN for explicit streams, and optional XPENDING for explicit stream=group pairs.
No redis-py dependency, no redis-cli subprocess, no Docker/log inspection.
"""

from __future__ import annotations

import socket
import time

from .. import config, core, schema


class RedisProtoError(Exception):
    pass


class Client:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.file = self.sock.makefile("rb")

    def close(self) -> None:
        try:
            self.file.close()
        finally:
            self.sock.close()

    def cmd(self, *parts: str):
        data = [f"*{len(parts)}\r\n".encode()]
        for part in parts:
            b = str(part).encode()
            data.append(f"${len(b)}\r\n".encode())
            data.append(b + b"\r\n")
        self.sock.sendall(b"".join(data))
        return self._read()

    def _line(self) -> bytes:
        line = self.file.readline()
        if not line:
            raise RedisProtoError("eof")
        return line.rstrip(b"\r\n")

    def _read(self):
        line = self._line()
        kind, body = line[:1], line[1:]
        if kind == b"+":
            return body.decode("utf-8", "replace")
        if kind == b"-":
            raise RedisProtoError(body.decode("utf-8", "replace"))
        if kind == b":":
            return int(body)
        if kind == b"$":
            n = int(body)
            if n < 0:
                return None
            data = self.file.read(n)
            self.file.read(2)
            return data.decode("utf-8", "replace")
        if kind == b"*":
            n = int(body)
            if n < 0:
                return None
            return [self._read() for _ in range(n)]
        raise RedisProtoError(f"unknown RESP type {kind!r}")


_ever_up = False  # has this instance ever answered? gates auto down-row recording


def _groups() -> dict[str, str]:
    out = {}
    for spec in config.REDIS_GROUPS:
        if "=" not in spec:
            continue
        stream, group = (s.strip() for s in spec.split("=", 1))
        if stream and group:
            out[stream] = group
    return out


def _used_memory_mb(info: str) -> float | None:
    val = _info_int(info, "used_memory")
    return round(val / 1e6, 1) if val is not None else None


def _info_int(info: str, key: str) -> int | None:
    """Pull one `key:value` integer out of an INFO section, tolerating absent keys."""
    prefix = key + ":"
    for line in info.splitlines():
        if line.startswith(prefix):
            try:
                return int(line.split(":", 1)[1])
            except (IndexError, ValueError):
                return None
    return None


def _info(c: Client, section: str) -> str:
    """One INFO section, swallowing protocol/socket errors so enrichment never breaks
    the core connectivity sample (older/locked servers may reject a section)."""
    try:
        return c.cmd("INFO", section) or ""
    except (OSError, RedisProtoError):
        return ""


def _pending(c: Client, stream: str, group: str) -> int | None:
    try:
        res = c.cmd("XPENDING", stream, group)
    except (OSError, RedisProtoError):
        return None
    if isinstance(res, list) and res:
        try:
            return int(res[0])
        except (TypeError, ValueError):
            return None
    return None


def collect(conn) -> None:
    global _ever_up
    if not config.REDIS_ENABLED:
        return
    ts = time.time()
    instance = f"{config.REDIS_HOST}:{config.REDIS_PORT}"
    rows = []
    try:
        c = Client(config.REDIS_HOST, config.REDIS_PORT, config.REDIS_TIMEOUT)
        try:
            c.cmd("PING")
            _ever_up = True
            mem = _used_memory_mb(_info(c, "memory"))
            clients = _info(c, "clients")
            stats = _info(c, "stats")
            rows.append({"ts": ts, "instance": instance, "stream": "__server__",
                         "connected": 1, "used_memory_mb": mem, "xlen": None, "pending": None,
                         "connected_clients": _info_int(clients, "connected_clients"),
                         "blocked_clients": _info_int(clients, "blocked_clients"),
                         "ops_per_sec": _info_int(stats, "instantaneous_ops_per_sec"),
                         "evicted_keys": _info_int(stats, "evicted_keys"),
                         "rejected_connections": _info_int(stats, "rejected_connections")})
            groups = _groups()
            for stream in config.REDIS_STREAMS:
                try:
                    xlen = int(c.cmd("XLEN", stream))
                except (OSError, RedisProtoError, TypeError, ValueError):
                    xlen = None
                rows.append({"ts": ts, "instance": instance, "stream": stream,
                             "connected": 1, "used_memory_mb": None, "xlen": xlen,
                             "pending": _pending(c, stream, groups[stream]) if stream in groups else None})
        finally:
            c.close()
    except (OSError, RedisProtoError) as e:
        # Auto mode: a Redis that has never answered on this node is simply not present, so
        # stay silent instead of spamming "redis down". Record the down row only when the
        # probe is explicitly forced on, or when this instance has answered before (a real
        # outage worth surfacing).
        if config.REDIS_FORCED or _ever_up:
            core.log(f"redis probe failed: {e.__class__.__name__}")
            rows.append({"ts": ts, "instance": instance, "stream": "__server__",
                         "connected": 0, "used_memory_mb": None, "xlen": None, "pending": None})
    if rows:
        schema.insert(conn, "redis_samples", rows)
        conn.commit()
