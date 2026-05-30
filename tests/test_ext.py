from smokemon import core, query, report, schema
from smokemon.probes import ext


class _Resp:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body, status=200, content_type="application/json"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _n):
        return self.body


def test_ext_json_collect_is_bounded_and_inserts_metrics(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(ext.config, "EXT_HTTP", ["app=http://127.0.0.1:8080/health|kind=json"])
    monkeypatch.setattr(ext.config, "EXT_MAX_METRICS", 10)
    monkeypatch.setattr(
        ext.urllib.request,
        "urlopen",
        lambda _req, timeout: _Resp(
            b'{"ok":true,"queue":{"depth":7},"redis":{"queues":{"scanner:fingerprint":{"xlen":5}}},'
            b'"version":"ignored"}'
        ),
    )
    ext.collect(conn)
    rows = conn.execute("SELECT source,metric,value,unit FROM ext_metrics ORDER BY metric").fetchall()
    conn.close()
    assert ("app", "up", 1.0, "") in rows
    assert any(r[0] == "app" and r[1] == "latency_ms" and r[2] >= 0.0 and r[3] == "ms" for r in rows)
    assert ("app", "queue_depth", 7.0, "") in rows
    assert ("app", "redis_queues_scanner_fingerprint_xlen", 5.0, "") in rows
    assert not any(r[1] == "version" for r in rows)


def test_ext_openmetrics_requires_allowlist(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(ext.config, "EXT_HTTP", [
        "node=http://127.0.0.1:9100/metrics|kind=metrics|metrics=node_load1"
    ])
    body = b"# HELP x y\nnode_load1 0.42\nprocess_resident_memory_bytes 123\n"
    monkeypatch.setattr(ext.urllib.request, "urlopen",
                        lambda _req, timeout: _Resp(body, content_type="text/plain"))
    ext.collect(conn)
    rows = conn.execute("SELECT metric,value FROM ext_metrics WHERE source='node' ORDER BY metric").fetchall()
    conn.close()
    assert ("node_load1", 0.42) in rows
    assert not any(metric == "process_resident_memory_bytes" for metric, _value in rows)


def test_ext_failure_records_down_event(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(ext.config, "EXT_HTTP", ["bad=http://127.0.0.1:9/health"])

    def fail(_req, timeout):
        raise OSError("nope")

    monkeypatch.setattr(ext.urllib.request, "urlopen", fail)
    ext.collect(conn)
    assert conn.execute("SELECT value FROM ext_metrics WHERE source='bad' AND metric='up'").fetchone()[0] == 0.0
    ev = conn.execute("SELECT source,event,detail FROM ext_events").fetchone()
    conn.close()
    assert ev == ("bad", "scrape-failed", "OSError")


def test_query_and_report_show_external_status(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "ext_metrics", [
        {"ts": ts0, "source": "app", "metric": "up", "value": 1.0, "unit": "", "labels": ""},
        {"ts": ts0, "source": "db", "metric": "up", "value": 0.0, "unit": "", "labels": ""},
    ])
    schema.insert(conn, "ext_events", [
        {"ts": ts0, "source": "db", "severity": "warn", "event": "scrape-failed", "detail": "TimeoutError"},
    ])
    conn.commit()
    assert query.load_ext_latest(conn, ts0 - 1, ts0 + 1)["db"]["up"]["value"] == 0.0
    assert query.load_ext_events(conn, ts0 - 1, ts0 + 1)[0]["source"] == "db"
    assert "ext db down" in report.status_line(conn, ts0 - 1, ts0 + 1)
    digest = report.digest(conn, ts0 - 1, ts0 + 1)
    conn.close()
    assert "External checks: db down." in digest
