"""X6 synthetic transactions: pure classification helpers + the additive schema table,
plus S5 self-footprint loader. No real network (the classifiers take raw inputs)."""

from smokemon import core, query, schema
from smokemon.probes import synthetic


def test_classify_captive():
    assert synthetic.classify_captive(204, "") == (True, "204 no content (clean)")
    ok, detail = synthetic.classify_captive(200, "<html>login</html>")
    assert not ok and "captive portal" in detail
    ok, detail = synthetic.classify_captive(302, "")
    assert not ok and "redirect" in detail
    ok, detail = synthetic.classify_captive(None, "")
    assert not ok and "no response" in detail


def test_doh_has_answer():
    good = '{"Status":0,"Answer":[{"name":"example.com","type":1,"data":"93.184.216.34"}]}'
    ok, detail = synthetic.doh_has_answer(good)
    assert ok and "93.184.216.34" in detail
    ok, detail = synthetic.doh_has_answer('{"Status":3,"Answer":[]}')   # NXDOMAIN
    assert not ok and "rcode 3" in detail
    ok, detail = synthetic.doh_has_answer('{"Status":0}')               # no answer key
    assert not ok
    assert synthetic.doh_has_answer("not json")[0] is False


def test_synthetic_table_present_after_init(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(synthetic_samples)")}
    assert {"ts", "probe", "ok", "latency_ms", "detail", "node"} <= cols
    schema.insert(conn, "synthetic_samples", [
        {"ts": 1.0, "probe": "doh", "ok": 1, "latency_ms": 12.0, "detail": "resolved"}])
    conn.commit()
    assert conn.execute("SELECT ok FROM synthetic_samples").fetchone()[0] == 1
    conn.close()


def test_load_self_footprint(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "proc_samples", [
        {"ts": ts0, "pid": 1, "name": "python3", "cpu_pct": 50.0, "rss_mb": 900.0},
        {"ts": ts0, "pid": 2, "name": "smokemon", "cpu_pct": 0.5, "rss_mb": 18.0},
        {"ts": ts0 + 30, "pid": 2, "name": "smokemon", "cpu_pct": 0.4, "rss_mb": 19.0}])
    conn.commit()
    d = query.load_self(conn, ts0 - 60, ts0 + 60)
    assert d["rss"] == [18.0, 19.0]      # only the smokemon rows, in time order
    assert d["cpu"] == [0.5, 0.4]
    conn.close()
