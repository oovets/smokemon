from smokemon import core, query, report, schema
from smokemon.probes import dockerps

# A small canned Docker Engine API surface. Tests patch the JSON transport so no
# real /var/run/docker.sock is touched; cgroup reads are patched separately.
_LIST = [
    {"Id": "aaa", "Names": ["/stadium-edge"], "Image": "stadium:main",
     "State": "running", "Status": "Up 3 days (healthy)"},
    {"Id": "bbb", "Names": ["/home-cam-mediamtx"], "Image": "mediamtx:latest",
     "State": "running", "Status": "Up 2 days"},
    {"Id": "ccc", "Names": ["/watchtower"], "Image": "watchtower",
     "State": "exited", "Status": "Exited (1) 4 months ago"},
    {"Id": "ddd", "Names": ["/stadium-jetson-app"], "Image": "stadium:old",
     "State": "exited", "Status": "Exited (0) 5 months ago"},
]
_INSPECT = {
    "aaa": {"RestartCount": 0, "State": {"ExitCode": 0, "OOMKilled": False,
                                         "Health": {"Status": "healthy"}}},
    "bbb": {"RestartCount": 2, "State": {"ExitCode": 0, "OOMKilled": False}},
    "ccc": {"RestartCount": 7, "State": {"ExitCode": 1, "OOMKilled": False}},
    "ddd": {"RestartCount": 0, "State": {"ExitCode": 0, "OOMKilled": False}},
}


def _fake_get_json(path):
    if path == "/containers/json?all=1":
        return _LIST
    for cid, body in _INSPECT.items():
        if path == f"/containers/{cid}/json":
            return body
    raise ValueError(f"unexpected docker path {path}")


def _enable(monkeypatch, **over):
    monkeypatch.setattr(dockerps.config, "DOCKER_ENABLED", True)
    # Force on so the socket-presence auto-gate doesn't skip in the test environment.
    monkeypatch.setattr(dockerps.config, "DOCKER_FORCED", True)
    monkeypatch.setattr(dockerps.config, "DOCKER_INSPECT", over.get("inspect", True))
    monkeypatch.setattr(dockerps.config, "DOCKER_CGROUP", over.get("cgroup", False))
    monkeypatch.setattr(dockerps.config, "DOCKER_MAX", over.get("max", 60))
    monkeypatch.setattr(dockerps, "_get_json", _fake_get_json)


def test_health_and_exit_parsing_from_status_string():
    assert dockerps._health_from_status("Up 3 days (healthy)") == "healthy"
    assert dockerps._health_from_status("Up 3 days (unhealthy)") == "unhealthy"
    assert dockerps._health_from_status("Up 1 min (health: starting)") == "starting"
    assert dockerps._health_from_status("Up 3 days") == ""
    assert dockerps._exit_from_status("Exited (137) 2 days ago") == 137
    assert dockerps._exit_from_status("Up 3 days") is None


def test_docker_probe_records_state_health_and_exit(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    _enable(monkeypatch)
    dockerps.collect(conn)
    latest = query.load_docker_latest(conn, 0, 10**12)
    conn.close()
    assert latest["stadium-edge"]["running"] == 1
    assert latest["stadium-edge"]["health"] == "healthy"
    assert latest["home-cam-mediamtx"]["restart_count"] == 2
    assert latest["watchtower"]["running"] == 0
    assert latest["watchtower"]["exit_code"] == 1
    assert latest["watchtower"]["restart_count"] == 7
    assert latest["stadium-jetson-app"]["exit_code"] == 0


def test_docker_cgroup_cpu_mem_sampled_for_running(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    _enable(monkeypatch, cgroup=True)
    monkeypatch.setattr(dockerps, "_cgroup_sample",
                        lambda cid, ts: {"cpu_pct": 12.5, "mem_mb": 280.5, "pids": 25})
    dockerps.collect(conn)
    latest = query.load_docker_latest(conn, 0, 10**12)
    conn.close()
    assert latest["stadium-edge"]["mem_mb"] == 280.5
    assert latest["stadium-edge"]["pids"] == 25
    # exited containers have no live cgroup, so cpu/mem stay NULL
    assert latest["watchtower"]["mem_mb"] is None


def test_docker_probe_records_daemon_down_when_forced(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(dockerps.config, "DOCKER_ENABLED", True)
    monkeypatch.setattr(dockerps.config, "DOCKER_FORCED", True)

    def boom(_path):
        raise OSError("no socket")

    monkeypatch.setattr(dockerps, "_get_json", boom)
    dockerps.collect(conn)
    latest = query.load_docker_latest(conn, 0, 10**12)
    conn.close()
    assert latest["__daemon__"]["running"] == 0


def test_docker_auto_is_noop_when_socket_absent(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(dockerps.config, "DOCKER_ENABLED", True)
    monkeypatch.setattr(dockerps.config, "DOCKER_FORCED", False)  # auto
    monkeypatch.setattr(dockerps.config, "DOCKER_SOCK", "/no/such/docker.sock")
    monkeypatch.setattr(dockerps, "_get_json", _fake_get_json)  # would succeed if reached
    dockerps.collect(conn)
    rows = conn.execute("SELECT count(*) FROM docker_samples").fetchone()[0]
    conn.close()
    assert rows == 0


def test_docker_probe_disabled_is_noop(tmp_db, monkeypatch):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    monkeypatch.setattr(dockerps.config, "DOCKER_ENABLED", False)
    dockerps.collect(conn)
    rows = conn.execute("SELECT count(*) FROM docker_samples").fetchone()[0]
    conn.close()
    assert rows == 0


def test_report_surfaces_docker_health(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "docker_samples", [
        {"ts": ts0, "name": "stadium-edge", "image": "x", "state": "running",
         "running": 1, "health": "healthy", "exit_code": 0, "restart_count": 0,
         "oom_killed": 0, "cpu_pct": 10.0, "mem_mb": 100.0, "pids": 5},
        {"ts": ts0, "name": "watchtower", "image": "x", "state": "exited",
         "running": 0, "health": "", "exit_code": 1, "restart_count": 7,
         "oom_killed": 0, "cpu_pct": None, "mem_mb": None, "pids": None},
    ])
    conn.commit()
    line = report.status_line(conn, ts0 - 1, ts0 + 1)
    digest = report.digest(conn, ts0 - 1, ts0 + 1)
    conn.close()
    assert "docker watchtower bad" in line
    assert "Docker: watchtower" in digest
