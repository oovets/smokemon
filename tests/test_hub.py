"""Hub ingest: first POST inserts, identical replay inserts zero (idempotent via
UNIQUE(node, src_id)), partial overlap inserts only the new rows."""

import time

import pytest

from smokemon import core, hub, schema


@pytest.fixture
def hub_ready(hub_db, monkeypatch):
    """Initialise a hub DB and wire it into the hub module's globals."""
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    monkeypatch.setattr(hub, "_conn", conn)
    hub._hub_cols.clear()
    hub._hub_cols.update({
        t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        for t in schema.STD_TABLES
    })
    yield conn
    conn.close()


def _payload(ts0):
    return {
        "node": "testnode",
        "tables": {
            "ping_runs": {
                "columns": ["id", "ts", "target", "sent", "recv", "loss_pct",
                            "rtt_min", "rtt_p25", "rtt_median", "rtt_p75",
                            "rtt_avg", "rtt_max", "rtt_stddev", "node"],
                "rows": [
                    [1, ts0, "1.1.1.1", 20, 20, 0.0, 5, 6, 7, 8, 7, 12, 1.5, "testnode"],
                    [2, ts0 + 10, "1.1.1.1", 20, 20, 0.0, 5, 6, 7, 8, 7, 12, 1.5, "testnode"],
                ],
            },
            "ping_rtts": {
                "columns": ["run_id", "rtt_ms"],
                "rows": [[1, 7.0], [1, 8.0], [2, 7.5]],
            },
            "net_samples": {
                "columns": ["id", "ts", "iface", "ibytes", "obytes", "ipkts", "opkts", "node"],
                "rows": [
                    [10, ts0, "eth0", 1000, 500, 0, 0, "testnode"],
                    [11, ts0 + 10, "eth0", 2000, 1500, 0, 0, "testnode"],
                ],
            },
            "host_samples": {
                "columns": ["id", "ts", "cpu_pct", "load1", "load5", "load15",
                            "mem_used_pct", "mem_total_mb", "temp_c",
                            "disk_read_mbps", "disk_write_mbps",
                            "swap_used_pct", "cache_mb", "oom_kill_count",
                            "psi_cpu", "psi_mem", "psi_io",
                            "cpu_freq_mhz", "cpu_throttle_count", "pi_throttle_bits", "node"],
                "rows": [[20, ts0, 5.0, 0.5, 0.4, 0.3, 30.0, 8192.0, 50.0,
                          1.0, 0.5, 0, 1000, 0, 0.1, 0.2, 0.3, 1500, 0, 0, "testnode"]],
            },
        },
    }


def test_first_ingest(hub_ready):
    ts0 = time.time()
    counts = hub.ingest(_payload(ts0))
    assert counts["ping_runs"] == 2
    assert counts["ping_rtts"] == 3
    assert counts["net_samples"] == 2
    assert counts["host_samples"] == 1


def test_idempotent_replay(hub_ready):
    ts0 = time.time()
    payload = _payload(ts0)
    hub.ingest(payload)
    counts2 = hub.ingest(payload)
    assert all(v == 0 for v in counts2.values()), counts2


def test_partial_overlap_inserts_only_new(hub_ready):
    ts0 = time.time()
    payload = _payload(ts0)
    hub.ingest(payload)
    payload["tables"]["ping_runs"]["rows"].append(
        [3, ts0 + 20, "1.1.1.1", 20, 19, 5.0, 5, 6, 7, 8, 7, 12, 1.5, "testnode"]
    )
    payload["tables"]["ping_rtts"]["rows"] = [[3, 7.2], [3, 7.8]]
    counts = hub.ingest(payload)
    assert counts["ping_runs"] == 1
    assert counts["ping_rtts"] == 2
    assert counts["net_samples"] == 0
    assert counts["host_samples"] == 0


def test_run_map_links_rtts_to_new_run_ids(hub_ready):
    ts0 = time.time()
    hub.ingest(_payload(ts0))
    # All 3 rtts should reference the two ping_run hub ids (not the src ids 1 and 2)
    hub_run_ids = {r[0] for r in hub_ready.execute("SELECT id FROM ping_runs").fetchall()}
    rtt_refs = {r[0] for r in hub_ready.execute("SELECT run_id FROM ping_rtts").fetchall()}
    assert rtt_refs.issubset(hub_run_ids), (rtt_refs, hub_run_ids)
