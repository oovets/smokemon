from smokemon import core, query, report, schema
from smokemon.probes import host, redisq


class _FakeRedis:
    def __init__(self, _host, _port, _timeout):
        self.closed = False

    def cmd(self, *parts):
        if parts == ("PING",):
            return "PONG"
        if parts == ("INFO", "memory"):
            return "used_memory:1234567\r\n"
        if parts == ("INFO", "clients"):
            return "connected_clients:11\r\nblocked_clients:2\r\n"
        if parts == ("INFO", "stats"):
            return "instantaneous_ops_per_sec:31\r\nevicted_keys:0\r\nrejected_connections:0\r\n"
        if parts == ("XLEN", "scanner:stats"):
            return 42
        if parts == ("XPENDING", "scanner:stats", "writers"):
            return [3, "0-1", "0-2", [["c", "3"]]]
        raise redisq.RedisProtoError("unexpected")

    def close(self):
        self.closed = True


def test_redis_probe_collects_memory_stream_and_pending(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(redisq.config, "REDIS_ENABLED", True)
    monkeypatch.setattr(redisq.config, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(redisq.config, "REDIS_PORT", 6379)
    monkeypatch.setattr(redisq.config, "REDIS_TIMEOUT", 0.1)
    monkeypatch.setattr(redisq.config, "REDIS_STREAMS", ["scanner:stats"])
    monkeypatch.setattr(redisq.config, "REDIS_GROUPS", ["scanner:stats=writers"])
    monkeypatch.setattr(redisq, "Client", _FakeRedis)
    redisq.collect(conn)
    latest = query.load_redis_latest(conn, 0, 10**12)
    conn.close()
    data = latest["127.0.0.1:6379"]
    assert data["connected"] == 1
    assert data["used_memory_mb"] == 1.2
    assert data["streams"]["scanner:stats"]["xlen"] == 42
    assert data["streams"]["scanner:stats"]["pending"] == 3
    assert data["connected_clients"] == 11
    assert data["blocked_clients"] == 2
    assert data["ops_per_sec"] == 31
    assert data["evicted_keys"] == 0
    assert data["rejected_connections"] == 0


def test_redis_probe_records_down_row_on_connect_failure(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(redisq.config, "REDIS_ENABLED", True)
    monkeypatch.setattr(redisq.config, "REDIS_HOST", "redis")
    monkeypatch.setattr(redisq.config, "REDIS_PORT", 6379)

    def fail(_host, _port, _timeout):
        raise OSError("no redis")

    monkeypatch.setattr(redisq, "Client", fail)
    redisq.collect(conn)
    assert query.load_redis_latest(conn, 0, 10**12)["redis:6379"]["connected"] == 0
    conn.close()


def test_jetson_gpu_sysfs_probe(monkeypatch):
    paths = ["/sys/class/devfreq/gpu.0"]
    values = {
        "/sys/class/devfreq/gpu.0/load": 256.0,
        "/sys/class/devfreq/gpu.0/cur_freq": 918000000.0,
    }
    monkeypatch.setattr(host.glob, "glob", lambda pattern: paths if pattern == "/sys/class/devfreq/*gpu*" else [])
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(host, "_read_float", lambda path, scale=1.0: values.get(path) / scale if path in values else None)
    assert host._jetson_gpu_linux() == [{"gpu": "gpu.0", "util_pct": 25.6, "freq_mhz": 918.0}]


def test_report_includes_redis_and_gpu(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "redis_samples", [
        {"ts": ts0, "instance": "127.0.0.1:6379", "stream": "__server__",
         "connected": 1, "used_memory_mb": 12.0, "xlen": None, "pending": None},
        {"ts": ts0, "instance": "127.0.0.1:6379", "stream": "scanner:stats",
         "connected": 1, "used_memory_mb": None, "xlen": 42, "pending": 3},
    ])
    schema.insert(conn, "gpu_samples", [
        {"ts": ts0, "gpu": "gpu.0", "util_pct": 55.0, "freq_mhz": 918.0},
    ])
    conn.commit()
    line = report.status_line(conn, ts0 - 1, ts0 + 1)
    digest = report.digest(conn, ts0 - 1, ts0 + 1)
    conn.close()
    assert "gpu 55%" in line
    assert "redis pending3" in line
    assert "GPU: peak 55%." in digest
    assert "Redis streams: scanner:stats xlen=42 pending=3." in digest
