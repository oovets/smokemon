from smokemon import core, query, report, schema
from smokemon.probes import pipeline


def _isolate(monkeypatch):
    """Reset the module-level cross-cycle state so tests don't leak into each other."""
    monkeypatch.setattr(pipeline, "_prev_ticks", {})
    monkeypatch.setattr(pipeline, "_prev_ts", 0.0)
    monkeypatch.setattr(pipeline, "_prev_start", {})
    monkeypatch.setattr(pipeline, "_restarts", {})
    monkeypatch.setattr(pipeline, "_CLK", 100)
    monkeypatch.setattr(pipeline, "_btime", lambda: 0.0)


def test_watch_spec_parsing():
    assert pipeline._watches(["gst=gst-launch-1.0", "bad", "app=python app.py"]) == [
        ("gst", "gst-launch-1.0"), ("app", "python app.py")]


def test_proc_watch_aggregates_count_rss_uptime(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    _isolate(monkeypatch)
    monkeypatch.setattr(pipeline.config, "PROC_WATCH", ["gst=gst-launch-1.0"])
    monkeypatch.setattr(pipeline.config, "RTSP_URLS", [])
    monkeypatch.setattr(pipeline, "_read_procs", lambda: [
        {"pid": 100, "start_ticks": 1000, "ticks": 500, "rss_mb": 50.0,
         "cmdline": "gst-launch-1.0 nvarguscamerasrc ! x264enc"},
        {"pid": 101, "start_ticks": 900, "ticks": 200, "rss_mb": 20.0,
         "cmdline": "python app.py"},
    ])
    pipeline.collect(conn, ts=10_000.0)
    watch = query.load_proc_watch(conn, 0, 10**12)
    conn.close()
    assert watch["gst"]["count"] == 1
    assert watch["gst"]["rss_mb"] == 50.0
    assert watch["gst"]["restarts"] == 0
    # uptime = now - (btime 0 + start_ticks/CLK) = 10000 - 10 = 9990
    assert watch["gst"]["uptime_s"] == 9990.0


def test_proc_watch_detects_restart_when_starttime_changes(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    _isolate(monkeypatch)
    monkeypatch.setattr(pipeline.config, "PROC_WATCH", ["gst=gst-launch-1.0"])
    monkeypatch.setattr(pipeline.config, "RTSP_URLS", [])
    procs = [{"pid": 100, "start_ticks": 1000, "ticks": 500, "rss_mb": 50.0,
              "cmdline": "gst-launch-1.0 cam"}]
    monkeypatch.setattr(pipeline, "_read_procs", lambda: procs)
    pipeline.collect(conn, ts=10_000.0)
    # pipeline restarted: same name, new pid + newer start_ticks
    procs[:] = [{"pid": 222, "start_ticks": 5000, "ticks": 10, "rss_mb": 48.0,
                 "cmdline": "gst-launch-1.0 cam"}]
    pipeline.collect(conn, ts=10_060.0)
    watch = query.load_proc_watch(conn, 0, 10**12)
    conn.close()
    assert watch["gst"]["restarts"] == 1


def test_rtsp_probe_ok_and_failure(monkeypatch):
    class _Sock:
        def __init__(self, resp):
            self._resp = resp

        def settimeout(self, _t):
            pass

        def sendall(self, _b):
            pass

        def recv(self, _n):
            return self._resp

        def close(self):
            pass

    monkeypatch.setattr(pipeline.socket, "create_connection",
                        lambda addr, timeout: _Sock(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n"))
    ok, latency, status = pipeline._rtsp_probe("rtsp://127.0.0.1:8554/imx519")
    assert ok == 1
    assert status == "200 OK"
    assert latency >= 0.0

    def boom(addr, timeout):
        raise OSError("refused")

    monkeypatch.setattr(pipeline.socket, "create_connection", boom)
    ok2, _latency2, status2 = pipeline._rtsp_probe("rtsp://127.0.0.1:8554/dead")
    assert ok2 == 0
    assert status2 == "OSError"


def test_rtsp_collect_records_stream_probe(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    _isolate(monkeypatch)
    monkeypatch.setattr(pipeline.config, "PROC_WATCH", [])
    monkeypatch.setattr(pipeline.config, "RTSP_URLS", ["cam=rtsp://127.0.0.1:8554/imx519"])
    monkeypatch.setattr(pipeline, "_rtsp_probe", lambda url: (1, 4.2, "200 OK"))
    pipeline.collect(conn, ts=10_000.0)
    streams = query.load_stream_probes(conn, 0, 10**12)
    conn.close()
    assert streams["cam"]["ok"] == 1
    assert streams["cam"]["latency_ms"] == 4.2


def test_report_surfaces_pipeline(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "proc_watch", [
        {"ts": ts0, "label": "gst", "count": 0, "cpu_pct": None, "rss_mb": None,
         "uptime_s": None, "restarts": 3},
    ])
    schema.insert(conn, "stream_probes", [
        {"ts": ts0, "url": "cam", "ok": 0, "latency_ms": None, "status": "TimeoutError"},
    ])
    conn.commit()
    line = report.status_line(conn, ts0 - 1, ts0 + 1)
    digest = report.digest(conn, ts0 - 1, ts0 + 1)
    conn.close()
    assert "pipeline" in line
    assert "gst down" in line or "gst" in line
    assert "Pipeline:" in digest
