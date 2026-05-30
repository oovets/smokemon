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
    for line in info.splitlines():
        if line.startswith("used_memory:"):
            try:
                return round(int(line.split(":", 1)[1]) / 1e6, 1)
            except (IndexError, ValueError):
                return None
    return None


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
    if not config.REDIS_ENABLED:
        return
    ts = time.time()
    instance = f"{config.REDIS_HOST}:{config.REDIS_PORT}"
    rows = []
    try:
        c = Client(config.REDIS_HOST, config.REDIS_PORT, config.REDIS_TIMEOUT)
        try:
            c.cmd("PING")
            mem = _used_memory_mb(c.cmd("INFO", "memory") or "")
            rows.append({"ts": ts, "instance": instance, "stream": "__server__",
                         "connected": 1, "used_memory_mb": mem, "xlen": None, "pending": None})
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
        core.log(f"redis probe failed: {e.__class__.__name__}")
        rows.append({"ts": ts, "instance": instance, "stream": "__server__",
                     "connected": 0, "used_memory_mb": None, "xlen": None, "pending": None})
    if rows:
        schema.insert(conn, "redis_samples", rows)
        conn.commit()
