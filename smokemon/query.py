"""Shared read-side: time window + data loaders for both renderers (TUI and PNG).
All loaders return raw epoch-second timestamps and accept an optional node filter
(required when reading a hub DB that holds multiple nodes)."""

import sqlite3
from datetime import datetime
from urllib.parse import urlparse

SKIP_IFACE_PREFIXES = ("lo", "gif", "stf", "anpi", "bridge", "ap", "veth", "docker", "br-", "virbr", "vnet", "tap")


def window(hours: float, minutes: float | None, since: str | None, until: str | None) -> tuple[float, float]:
    u = datetime.fromisoformat(until).timestamp() if until else datetime.now().timestamp()
    if since:
        s = datetime.fromisoformat(since).timestamp()
    elif minutes is not None:
        s = u - minutes * 60
    else:
        s = u - hours * 3600
    return s, u


def host_label(url: str) -> str:
    h = urlparse(url).netloc.replace("www.", "")
    return h.rsplit(".", 1)[0] if "." in h else (h or url)  # strip domain suffix (.com etc.)


def _q(conn, sql: str, params):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _filt(node: str | None) -> tuple[str, list]:
    return (" AND node=?", [node]) if node else ("", [])


def open_ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def load_ping_agg(conn, since, until, targets, node=None):
    nf, np_ = _filt(node)
    q = "SELECT ts,target,loss_pct,rtt_min,rtt_median,rtt_max FROM ping_runs WHERE ts BETWEEN ? AND ?" + nf
    params = [since, until, *np_]
    if targets:
        q += " AND target IN (%s)" % ",".join("?" * len(targets))
        params += targets
    q += " ORDER BY target, ts"
    data: dict[str, dict] = {}
    for ts, target, loss, rmin, rmed, rmax in _q(conn, q, params):
        d = data.setdefault(target, {"t": [], "min": [], "med": [], "max": [], "loss": []})
        d["t"].append(ts); d["min"].append(rmin); d["med"].append(rmed); d["max"].append(rmax); d["loss"].append(loss)
    return data


def load_ping_smoke(conn, since, until, targets, node=None):
    import numpy as np
    nf, np_ = _filt(node)
    q = "SELECT id,ts,target,loss_pct FROM ping_runs WHERE ts BETWEEN ? AND ?" + nf
    params = [since, until, *np_]
    if targets:
        q += " AND target IN (%s)" % ",".join("?" * len(targets))
        params += targets
    q += " ORDER BY ts"
    runs = _q(conn, q, params)
    if not runs:
        return {}
    rtts: dict[int, list[float]] = {r[0]: [] for r in runs}
    ids = list(rtts)
    for i in range(0, len(ids), 800):  # batch around SQLite's variable limit
        chunk = ids[i:i + 800]
        for rid, rtt in conn.execute(
                "SELECT run_id,rtt_ms FROM ping_rtts WHERE run_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            if rid in rtts:
                rtts[rid].append(rtt)
    data: dict[str, dict] = {}
    for rid, ts, target, loss in runs:
        d = data.setdefault(target, {"t": [], "p0": [], "p25": [], "p50": [], "p75": [], "p100": [], "loss": []})
        d["t"].append(ts); d["loss"].append(loss)
        s = rtts.get(rid)
        for k, pc in (("p0", 0), ("p25", 25), ("p50", 50), ("p75", 75), ("p100", 100)):
            d[k].append(float(np.percentile(s, pc)) if s else float("nan"))
    return data


def load_net(conn, since, until, node=None):
    nf, np_ = _filt(node)
    rows = _q(conn, "SELECT ts,iface,ibytes,obytes FROM net_samples WHERE ts BETWEEN ? AND ?" + nf
              + " ORDER BY iface, ts", [since, until, *np_])
    series, prev = {}, {}
    for ts, iface, ib, ob in rows:
        if iface.startswith(SKIP_IFACE_PREFIXES):
            continue
        if iface in prev:
            pts, pib, pob = prev[iface]
            dt, din, dout = ts - pts, ib - prev[iface][1], ob - prev[iface][2]
            if dt > 0 and din >= 0 and dout >= 0:
                s = series.setdefault(iface, {"t": [], "in": [], "out": []})
                s["t"].append(ts); s["in"].append(din * 8 / 1e6 / dt); s["out"].append(dout * 8 / 1e6 / dt)
        prev[iface] = (ts, ib, ob)
    return {i: s for i, s in series.items() if s["in"] and (max(s["in"]) > 0.01 or max(s["out"]) > 0.01)}


def load_http(conn, since, until, node=None):
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, url, ttfb in _q(conn, "SELECT ts,url,ttfb_ms FROM http_samples WHERE ts BETWEEN ? AND ?" + nf
                            + " ORDER BY url, ts", [since, until, *np_]):
        d = data.setdefault(url, {"t": [], "ttfb": []})
        d["t"].append(ts); d["ttfb"].append(ttfb)
    return data


def load_mtr(conn, since, until, node=None):
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, target, hop, host, loss, avg in _q(
            conn, "SELECT ts,target,hop_no,host,loss_pct,avg_ms FROM mtr_hops WHERE ts BETWEEN ? AND ?" + nf
            + " ORDER BY ts,hop_no", [since, until, *np_]):
        h = data.setdefault(target, {}).setdefault(hop, {"t": [], "avg": [], "host": host, "loss": []})
        h["t"].append(ts); h["avg"].append(avg); h["loss"].append(loss); h["host"] = host
    return data


def load_wifi(conn, since, until, node=None):
    nf, np_ = _filt(node)
    d = {"t": [], "rssi": [], "noise": [], "tx": []}
    for ts, rssi, noise, tx in _q(conn, "SELECT ts,rssi_dbm,noise_dbm,tx_rate_mbps FROM wifi_samples "
                                  "WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts", [since, until, *np_]):
        d["t"].append(ts); d["rssi"].append(rssi); d["noise"].append(noise); d["tx"].append(tx)
    return d if d["t"] else {}


def load_iperf(conn, since, until, node=None):
    nf, np_ = _filt(node)
    d = {"t": [], "up": [], "down": []}
    for ts, up, down in _q(conn, "SELECT ts,up_mbps,down_mbps FROM iperf_samples WHERE ts BETWEEN ? AND ?" + nf
                           + " ORDER BY ts", [since, until, *np_]):
        d["t"].append(ts); d["up"].append(up); d["down"].append(down)
    return d if d["t"] else {}


def load_host(conn, since, until, node=None):
    nf, np_ = _filt(node)
    d = {"t": [], "cpu": [], "load1": [], "mem": [], "temp": []}
    for ts, cpu, load1, mem, temp in _q(conn, "SELECT ts,cpu_pct,load1,mem_used_pct,temp_c FROM host_samples "
                                        "WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts", [since, until, *np_]):
        d["t"].append(ts); d["cpu"].append(cpu); d["load1"].append(load1); d["mem"].append(mem); d["temp"].append(temp)
    return d if d["t"] else {}


def load_disk(conn, since, until, node=None):
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, mount, used in _q(conn, "SELECT ts,mount,used_pct FROM disk_samples WHERE ts BETWEEN ? AND ?" + nf
                              + " ORDER BY mount, ts", [since, until, *np_]):
        d = data.setdefault(mount, {"t": [], "used": []})
        d["t"].append(ts); d["used"].append(used)
    return data
