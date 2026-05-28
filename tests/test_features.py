"""QW1 bufferbloat grade, QW2 HTTP layer-blame, QW4 death-clock forecasts.

Pure render-side analysis helpers in smokemon.query plus the iperf probe's stream-RTT
parser. All stdlib, no real network: helpers are exercised directly and the two
extended loaders (load_iperf, load_http) are checked for their new shape against a
temp DB."""

import math

from smokemon import core, query, schema
from smokemon.probes import iperf

# ---------- QW1: bufferbloat ----------

def test_bufferbloat_grade_thresholds():
    assert query.bufferbloat_grade(0.0) == "A+"
    assert query.bufferbloat_grade(4.9) == "A+"
    assert query.bufferbloat_grade(5.0) == "A"
    assert query.bufferbloat_grade(29.9) == "A"
    assert query.bufferbloat_grade(30.0) == "B"
    assert query.bufferbloat_grade(59.9) == "B"
    assert query.bufferbloat_grade(60.0) == "C"
    assert query.bufferbloat_grade(199.0) == "C"
    assert query.bufferbloat_grade(200.0) == "D"
    assert query.bufferbloat_grade(399.0) == "D"
    assert query.bufferbloat_grade(400.0) == "F"
    assert query.bufferbloat_grade(5000.0) == "F"


def test_idle_rtt_picks_max_target_median_and_skips_nan():
    # 'med' (agg shape): two targets, max of medians is the WAN path (20).
    ping_agg = {"gw": {"med": [4.0, 4.0, 6.0]}, "internet": {"med": [18.0, 20.0, 22.0]}}
    assert query.idle_rtt_ms(ping_agg) == 20.0
    # 'p50' (smoke shape) with NaN holes must be ignored.
    nan = float("nan")
    ping_smoke = {"internet": {"p50": [nan, 10.0, nan, 12.0]}}
    assert query.idle_rtt_ms(ping_smoke) == 11.0
    assert query.idle_rtt_ms({}) is None


def test_bufferbloat_combines_loaded_and_idle():
    ping = {"internet": {"med": [10.0, 10.0, 10.0]}}
    # loaded 70ms, idle 10ms -> added 60ms -> grade C.
    iperf_data = {"rtt_load": [None, 70.0]}
    grade, added, loaded = query.bufferbloat(iperf_data, ping)
    assert grade == "C" and added == 60.0 and loaded == 70.0


def test_bufferbloat_clamps_negative_added():
    # iperf server closer than the slowest ping target: added clamps to 0 -> A+.
    ping = {"internet": {"med": [50.0]}}
    grade, added, loaded = query.bufferbloat({"rtt_load": [12.0]}, ping)
    assert added == 0.0 and grade == "A+" and loaded == 12.0


def test_bufferbloat_none_without_loaded_rtt():
    assert query.bufferbloat({"rtt_load": [None, None]}, {"internet": {"med": [10.0]}}) is None
    assert query.bufferbloat({}, {}) is None


def test_iperf_probe_parses_stream_rtt_microseconds_to_ms():
    data = {"end": {"streams": [
        {"sender": {"mean_rtt": 12000}},   # 12 ms
        {"sender": {"mean_rtt": 18000}},   # 18 ms
    ]}}
    assert iperf._under_load_rtt_ms(data) == 15.0  # mean of 12 and 18


def test_iperf_probe_rtt_none_when_unavailable():
    assert iperf._under_load_rtt_ms(None) is None
    assert iperf._under_load_rtt_ms({"end": {"streams": []}}) is None
    # UDP / platforms without tcp_info report 0 -> treated as absent.
    assert iperf._under_load_rtt_ms({"end": {"streams": [{"sender": {"mean_rtt": 0}}]}}) is None


# ---------- QW2: HTTP layer-blame ----------

def test_http_phases_https_decomposition():
    # cumulative curl timestamps: dns=10, connect=30, tls=70, ttfb=120.
    ph = query.http_phases(10.0, 30.0, 70.0, 120.0)
    assert ph == {"dns": 10.0, "connect": 20.0, "tls": 40.0, "server": 50.0}


def test_http_phases_plaintext_has_no_tls():
    # tls=0 (http://): the TLS phase is zero and server wait is measured from connect.
    ph = query.http_phases(5.0, 15.0, 0.0, 60.0)
    assert ph["tls"] == 0.0
    assert ph["server"] == 45.0  # 60 - 15


def test_http_phases_none_safe():
    assert query.http_phases(None, None, None, None) == {"dns": 0.0, "connect": 0.0, "tls": 0.0, "server": 0.0}


def test_http_blame_names_dominant_layer():
    # one URL, server wait dominates.
    http_data = {"https://x": {"t": [1.0], "dns": [5.0], "connect": [10.0], "tls": [20.0], "ttfb": [200.0]}}
    layer, ms = query.http_blame(http_data)
    assert layer == "server" and ms == 180.0  # 200 - 20
    assert query.http_blame({}) is None


# ---------- QW4: death clocks ----------

def test_linear_eta_rising_series():
    # used% rising 1/sec from 90; 10 more to hit 100 -> 10 seconds.
    t = [0.0, 1.0, 2.0, 3.0]
    vals = [87.0, 88.0, 89.0, 90.0]
    eta = query.linear_eta_seconds(t, vals, 100.0)
    assert math.isclose(eta, 10.0, rel_tol=1e-6)


def test_linear_eta_none_for_flat_or_declining():
    assert query.linear_eta_seconds([0.0, 1.0, 2.0], [50.0, 50.0, 50.0], 100.0) is None
    assert query.linear_eta_seconds([0.0, 1.0, 2.0], [50.0, 40.0, 30.0], 100.0) is None


def test_linear_eta_none_when_too_few_points():
    assert query.linear_eta_seconds([0.0, 1.0], [10.0, 20.0], 100.0) is None


def test_linear_eta_zero_when_already_past_target():
    assert query.linear_eta_seconds([0.0, 1.0, 2.0], [99.0, 100.0, 101.0], 100.0) == 0.0


def test_human_eta_ranges():
    assert query.human_eta(None) == ""
    assert query.human_eta(0) == "now"
    assert query.human_eta(90) == "~2m"
    assert query.human_eta(3600 * 6) == "~6h"
    assert query.human_eta(86400 * 14) == "~14d"
    assert query.human_eta(86400 * 365 * 3) == "~3y"


def test_disk_full_and_wear_eta_pick_soonest():
    disk = {
        "/":     {"t": [0.0, 1.0, 2.0], "used": [80.0, 81.0, 82.0]},   # +1/s, 18 left -> 18s
        "/data": {"t": [0.0, 1.0, 2.0], "used": [50.0, 55.0, 60.0]},   # +5/s, 40 left -> 8s (soonest)
    }
    mount, eta = query.disk_full_eta(disk)
    assert mount == "/data" and math.isclose(eta, 8.0, rel_tol=1e-6)

    health = {"mmcblk0": {"t": [0.0, 1.0, 2.0], "wear": [40.0, 42.0, 44.0]}}  # +2/s, 56 left -> 28s
    dev, weta = query.wear_eta(health)
    assert dev == "mmcblk0" and math.isclose(weta, 28.0, rel_tol=1e-6)

    assert query.disk_full_eta({}) is None
    assert query.wear_eta({}) is None


# ---------- extended loaders (DB-backed) ----------

def test_load_iperf_includes_rtt_load(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "iperf_samples", [{
        "ts": ts0, "server": "host", "up_mbps": 900.0, "down_mbps": 940.0,
        "retransmits": 0, "rtt_under_load_ms": 42.0,
    }])
    conn.commit()
    d = query.load_iperf(conn, ts0 - 60, ts0 + 60)
    assert d["rtt_load"] == [42.0]
    assert d["up"] == [900.0] and d["down"] == [940.0]
    conn.close()


def test_load_http_includes_phase_columns(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "http_samples", [{
        "ts": ts0, "url": "https://example.com", "http_code": 200,
        "dns_ms": 10.0, "connect_ms": 30.0, "tls_ms": 70.0, "ttfb_ms": 120.0, "total_ms": 130.0,
    }])
    conn.commit()
    data = query.load_http(conn, ts0 - 60, ts0 + 60)
    d = data["https://example.com"]
    assert d["dns"] == [10.0] and d["connect"] == [30.0] and d["tls"] == [70.0] and d["ttfb"] == [120.0]
    # blame on this single sample: server wait (120 - 70 = 50) dominates.
    assert query.http_blame(data) == ("server", 50.0)
    conn.close()
