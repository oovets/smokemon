"""Shared read-side: time window + data loaders for both renderers (TUI and PNG).
All loaders return raw epoch-second timestamps and accept an optional node filter
(required when reading a hub DB that holds multiple nodes)."""

import sqlite3
from datetime import datetime
from urllib.parse import urlparse

SKIP_IFACE_PREFIXES = ("lo", "gif", "stf", "anpi", "bridge", "ap", "veth", "docker", "br-", "virbr", "vnet", "tap")

# LAG() window function arrived in SQLite 3.25 (2018). Used by load_net for in-SQL
# delta computation; older runtimes fall back to the Python loop further below.
_HAS_LAG = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:3]) >= (3, 25, 0)


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


def last_value(seq):
    """Most recent non-None value in a time-ordered sequence, or None. Renderers use
    this for the 'current' annotation (temp, tx rate, last watt, conntrack %)."""
    return next((v for v in reversed(seq) if v is not None), None)


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
        # loss_pct is always set by the collector, but hub-imported / legacy rows may carry
        # NULL; coerce to 0.0 so the renderers' sum()/nanmean() never choke on a None.
        d["t"].append(ts); d["min"].append(rmin); d["med"].append(rmed); d["max"].append(rmax)
        d["loss"].append(loss if loss is not None else 0.0)
    return data


def load_ping_smoke(conn, since, until, targets, node=None):
    """Smoke-plot percentiles. Reads pre-aggregated rtt_min/p25/median/p75/max directly
    from ping_runs (added in the rtt_p25/p75 migration). For rows older than the
    migration (rtt_p25 IS NULL), falls back to a single JOIN against ping_rtts to
    rebuild p25/p75 on the fly. p0/p100 are always rtt_min/rtt_max."""
    nf, np_ = _filt(node)
    q = ("SELECT id, ts, target, loss_pct, rtt_min, rtt_p25, rtt_median, rtt_p75, rtt_max "
         "FROM ping_runs WHERE ts BETWEEN ? AND ?" + nf)
    params = [since, until, *np_]
    if targets:
        q += " AND target IN (%s)" % ",".join("?" * len(targets))
        params += targets
    q += " ORDER BY ts"
    runs = _q(conn, q, params)
    if not runs:
        return {}

    legacy_ids = [r[0] for r in runs if (r[5] is None or r[7] is None) and r[6] is not None]
    legacy = _percentiles_for(conn, legacy_ids) if legacy_ids else {}

    data: dict[str, dict] = {}
    nan = float("nan")
    for rid, ts, target, loss, rmin, rp25, rmed, rp75, rmax in runs:
        d = data.setdefault(target, {"t": [], "p0": [], "p25": [], "p50": [], "p75": [], "p100": [], "loss": []})
        d["t"].append(ts); d["loss"].append(loss)
        if (rp25 is None or rp75 is None) and rmed is not None:
            rp25, rp75 = legacy.get(rid, (rmed, rmed))
        d["p0"].append(rmin if rmin is not None else nan)
        d["p25"].append(rp25 if rp25 is not None else nan)
        d["p50"].append(rmed if rmed is not None else nan)
        d["p75"].append(rp75 if rp75 is not None else nan)
        d["p100"].append(rmax if rmax is not None else nan)
    return data


def _percentiles_for(conn, ids: list[int]) -> dict[int, tuple[float, float]]:
    """For legacy rows lacking rtt_p25/p75: pull raw rtts (chunked to stay under SQLite's
    IN-list variable limit) and compute p25/p75 once per id. Read-only, no temp table."""
    import statistics
    raw: dict[int, list[float]] = {}
    for i in range(0, len(ids), 800):
        chunk = ids[i:i + 800]
        for rid, rtt in conn.execute(
                "SELECT run_id, rtt_ms FROM ping_rtts WHERE run_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            raw.setdefault(rid, []).append(rtt)
    out: dict[int, tuple[float, float]] = {}
    for rid, vals in raw.items():
        if len(vals) >= 2:
            p25, _p50, p75 = statistics.quantiles(vals, n=4)
        else:
            p25 = p75 = vals[0]
        out[rid] = (p25, p75)
    return out


def load_net(conn, since, until, node=None):
    """Per-interface bandwidth. Delta computation runs in SQL via LAG() when available
    (avoids materializing all rows in Python); falls back to the original Python loop
    on SQLite < 3.25."""
    nf, np_ = _filt(node)
    if _HAS_LAG:
        rows = _q(conn,
                  "SELECT ts, iface, ts - LAG(ts) OVER w AS dt, "
                  "ibytes - LAG(ibytes) OVER w AS din, obytes - LAG(obytes) OVER w AS dout "
                  "FROM net_samples WHERE ts BETWEEN ? AND ?" + nf + " "
                  "WINDOW w AS (PARTITION BY iface ORDER BY ts) ORDER BY iface, ts",
                  [since, until, *np_])
        series: dict[str, dict] = {}
        for ts, iface, dt, din, dout in rows:
            if dt is None or dt <= 0 or din is None or dout is None or din < 0 or dout < 0:
                continue
            if iface.startswith(SKIP_IFACE_PREFIXES):
                continue
            s = series.setdefault(iface, {"t": [], "in": [], "out": []})
            s["t"].append(ts)
            s["in"].append(din * 8 / 1e6 / dt)
            s["out"].append(dout * 8 / 1e6 / dt)
        return {i: s for i, s in series.items() if s["in"] and (max(s["in"]) > 0.01 or max(s["out"]) > 0.01)}

    rows = _q(conn, "SELECT ts,iface,ibytes,obytes FROM net_samples WHERE ts BETWEEN ? AND ?" + nf
              + " ORDER BY iface, ts", [since, until, *np_])
    series, prev = {}, {}
    for ts, iface, ib, ob in rows:
        if iface.startswith(SKIP_IFACE_PREFIXES):
            continue
        if iface in prev:
            pts, pib, pob = prev[iface]
            dt, din, dout = ts - pts, ib - pib, ob - pob
            if dt > 0 and din >= 0 and dout >= 0:
                s = series.setdefault(iface, {"t": [], "in": [], "out": []})
                s["t"].append(ts); s["in"].append(din * 8 / 1e6 / dt); s["out"].append(dout * 8 / 1e6 / dt)
        prev[iface] = (ts, ib, ob)
    return {i: s for i, s in series.items() if s["in"] and (max(s["in"]) > 0.01 or max(s["out"]) > 0.01)}


def load_http(conn, since, until, node=None):
    """TTFB series per URL plus curl's cumulative phase timestamps (dns/connect/tls),
    so the renderers can name the dominant latency layer via http_blame()."""
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, url, dns, connect, tls, ttfb in _q(conn,
            "SELECT ts,url,dns_ms,connect_ms,tls_ms,ttfb_ms FROM http_samples WHERE ts BETWEEN ? AND ?" + nf
            + " ORDER BY url, ts", [since, until, *np_]):
        d = data.setdefault(url, {"t": [], "ttfb": [], "dns": [], "connect": [], "tls": []})
        d["t"].append(ts); d["ttfb"].append(ttfb)
        d["dns"].append(dns); d["connect"].append(connect); d["tls"].append(tls)
    return data


# ---------- QW2: HTTP layer-blame ----------

HTTP_LAYER_LABELS = {"dns": "DNS", "connect": "TCP connect", "tls": "TLS", "server": "server wait"}


def http_phases(dns, connect, tls, ttfb) -> dict[str, float]:
    """Decompose curl's cumulative timestamps into per-phase durations (ms). curl's
    time_namelookup/time_connect/time_appconnect/time_starttransfer are all measured
    from request start, so each phase is the gap to the previous milestone. tls<=0
    means a plaintext request (no TLS phase). None-safe."""
    dns = dns or 0.0; connect = connect or 0.0; tls = tls or 0.0; ttfb = ttfb or 0.0
    after_connect = tls if tls > 0 else connect  # TLS handshake follows the TCP connect
    return {
        "dns": dns,
        "connect": max(0.0, connect - dns),
        "tls": max(0.0, tls - connect) if tls > 0 else 0.0,
        "server": max(0.0, ttfb - after_connect),
    }


def http_blame(http_data: dict) -> tuple[str, float] | None:
    """Name the dominant latency layer across the most recent sample of each URL.
    Returns (layer_key, avg_ms_for_that_layer) or None when there is no data."""
    totals = {"dns": 0.0, "connect": 0.0, "tls": 0.0, "server": 0.0}
    n = 0
    for d in http_data.values():
        if not d.get("t"):
            continue
        ph = http_phases(d["dns"][-1], d["connect"][-1], d["tls"][-1], d["ttfb"][-1])
        for k in totals:
            totals[k] += ph[k]
        n += 1
    if not n:
        return None
    layer = max(totals, key=totals.get)
    return layer, totals[layer] / n


# ---------- QW1: bufferbloat grade ----------

# dslreports-style grade by latency *added* under load (ms). Below the first
# threshold is the best grade; at or above the last is "F".
_BUFFERBLOAT_GRADES = ((5.0, "A+"), (30.0, "A"), (60.0, "B"), (200.0, "C"), (400.0, "D"))


def bufferbloat_grade(added_ms: float) -> str:
    for thr, grade in _BUFFERBLOAT_GRADES:
        if added_ms < thr:
            return grade
    return "F"


def idle_rtt_ms(ping_data: dict) -> float | None:
    """Idle-latency proxy for the bufferbloat baseline: the largest per-target
    median-of-medians across the ping window. The max picks the WAN/internet path
    (gateway/LAN medians are near zero) so it pairs sensibly with the loaded RTT iperf
    measures to an off-net server. Reads either the agg ('med') or smoke ('p50') shape
    and skips None/NaN. Returns None when no finite median exists."""
    import statistics
    per_target = []
    for d in ping_data.values():
        meds = [m for m in (d.get("med") or d.get("p50") or []) if m is not None and m == m]
        if meds:
            per_target.append(statistics.median(meds))
    return max(per_target) if per_target else None


def bufferbloat(iperf_data: dict, ping_data: dict) -> tuple[str, float, float] | None:
    """(grade, added_ms, loaded_ms) comparing the most recent loaded RTT against the
    idle ping baseline, or None when either is unavailable. added is clamped at 0 (an
    off-net iperf server can sit closer than the slowest ping target)."""
    loaded = last_value(iperf_data.get("rtt_load", [])) if iperf_data else None
    if loaded is None:
        return None
    idle = idle_rtt_ms(ping_data) or 0.0
    added = max(0.0, loaded - idle)
    return bufferbloat_grade(added), added, loaded


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
    """RSSI/noise/tx (gauges) + retry/discard/beacon (kernel counters -> per-second deltas)
    + BSSID (last seen + total distinct count, exposed as `roams`)."""
    nf, np_ = _filt(node)
    rows = _q(conn, "SELECT ts, rssi_dbm, noise_dbm, tx_rate_mbps, bssid, retry_count, "
              "discard_count, beacon_loss FROM wifi_samples WHERE ts BETWEEN ? AND ?" + nf
              + " ORDER BY ts", [since, until, *np_])
    if not rows:
        return {}
    d: dict = {"t": [], "rssi": [], "noise": [], "tx": [],
               "retry_rate": [], "discard_rate": [], "beacon_rate": [],
               "bssid": [], "roams": 0}
    prev_retry = prev_disc = prev_beacon = prev_ts = None
    seen_bssid: set[str] = set()
    last_bssid: str | None = None
    for ts, rssi, noise, tx, bssid, retry, disc, beacon in rows:
        d["t"].append(ts); d["rssi"].append(rssi); d["noise"].append(noise); d["tx"].append(tx)
        d["bssid"].append(bssid)
        if bssid:
            if last_bssid and bssid != last_bssid:
                d["roams"] += 1
            last_bssid = bssid
            seen_bssid.add(bssid)
        d["retry_rate"].append(_rate(retry, prev_retry, ts, prev_ts))
        d["discard_rate"].append(_rate(disc, prev_disc, ts, prev_ts))
        d["beacon_rate"].append(_rate(beacon, prev_beacon, ts, prev_ts))
        prev_retry, prev_disc, prev_beacon, prev_ts = retry, disc, beacon, ts
    d["bssids_seen"] = len(seen_bssid)
    return d


def load_iperf(conn, since, until, node=None):
    """Throughput series + the loaded TCP RTT (rtt_under_load_ms, NULL on rows that
    predate the bufferbloat migration or on platforms without tcp_info)."""
    nf, np_ = _filt(node)
    d = {"t": [], "up": [], "down": [], "rtt_load": []}
    for ts, up, down, rtt in _q(conn,
            "SELECT ts,up_mbps,down_mbps,rtt_under_load_ms FROM iperf_samples WHERE ts BETWEEN ? AND ?" + nf
            + " ORDER BY ts", [since, until, *np_]):
        d["t"].append(ts); d["up"].append(up); d["down"].append(down); d["rtt_load"].append(rtt)
    return d if d["t"] else {}


def load_host(conn, since, until, node=None):
    """Combined host gauges. New fields (swap/cache/freq/throttle/pi_bits) default to
    None when reading older rows that predate the schema migration."""
    nf, np_ = _filt(node)
    d = {"t": [], "cpu": [], "load1": [], "mem": [], "temp": [], "swap": [], "cache_mb": []}
    rows = _q(conn, "SELECT ts, cpu_pct, load1, mem_used_pct, temp_c, swap_used_pct, cache_mb "
              "FROM host_samples WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts", [since, until, *np_])
    for ts, cpu, load1, mem, temp, swap, cache in rows:
        d["t"].append(ts); d["cpu"].append(cpu); d["load1"].append(load1)
        d["mem"].append(mem); d["temp"].append(temp); d["swap"].append(swap); d["cache_mb"].append(cache)
    return d if d["t"] else {}


def load_disk(conn, since, until, node=None):
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, mount, used, inode in _q(conn, "SELECT ts, mount, used_pct, inode_used_pct FROM disk_samples "
                                     "WHERE ts BETWEEN ? AND ?" + nf
                                     + " ORDER BY mount, ts", [since, until, *np_]):
        d = data.setdefault(mount, {"t": [], "used": [], "inode": []})
        d["t"].append(ts); d["used"].append(used); d["inode"].append(inode)
    return data


# ---------- new loaders for the v0.11 metrics ----------

# Raspberry Pi `vcgencmd get_throttled` bit field. Bits 0-3 are live conditions,
# bits 16-19 are the same conditions latched ("sticky") since boot. Shared by both
# renderers so the label set cannot drift between PNG and TUI.
PI_THROTTLE_BITS = {
    0: "uv-now", 1: "freq-cap-now", 2: "throttled-now", 3: "soft-temp-now",
    16: "uv-since-boot", 17: "freq-cap-since-boot",
    18: "throttled-since-boot", 19: "soft-temp-since-boot",
}


def pi_bits_seen(bits_list) -> list[str]:
    """OR every sampled pi_throttle_bits value in the window and return the human labels
    for each bit ever set. Renderers format the list (PNG joins with ', '; TUI too)."""
    seen = 0
    for b in bits_list:
        if b is not None:
            seen |= int(b)
    return [name for bit, name in PI_THROTTLE_BITS.items() if seen & (1 << bit)]


def _rate(curr, prev, ts, prev_ts):
    """events/second delta between two counter samples. Returns None on missing data,
    counter resets (negative), or zero/negative dt. Used for retransmits, retries, etc."""
    if curr is None or prev is None or prev_ts is None:
        return None
    dt = ts - prev_ts
    if dt <= 0:
        return None
    diff = curr - prev
    if diff < 0:
        return None
    return diff / dt


def load_psi(conn, since, until, node=None):
    """Linux PSI 'some' avg10 values for CPU/memory/IO. % time at least one task
    was blocked waiting on the resource."""
    nf, np_ = _filt(node)
    d: dict = {"t": [], "cpu": [], "mem": [], "io": []}
    for ts, c, m, i in _q(conn, "SELECT ts, psi_cpu, psi_mem, psi_io FROM host_samples "
                          "WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts", [since, until, *np_]):
        if c is None and m is None and i is None:
            continue
        d["t"].append(ts); d["cpu"].append(c); d["mem"].append(m); d["io"].append(i)
    return d if d["t"] else {}


def load_freq(conn, since, until, node=None):
    """CPU mean frequency + throttle counters + Pi sticky under-voltage bits."""
    nf, np_ = _filt(node)
    d: dict = {"t": [], "mhz": [], "throttle": [], "pi_bits": []}
    rows = _q(conn, "SELECT ts, cpu_freq_mhz, cpu_throttle_count, pi_throttle_bits "
              "FROM host_samples WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts",
              [since, until, *np_])
    prev_throttle = prev_ts = None
    for ts, mhz, throttle, bits in rows:
        if mhz is None and throttle is None and bits is None:
            continue
        d["t"].append(ts); d["mhz"].append(mhz); d["pi_bits"].append(bits)
        d["throttle"].append(_rate(throttle, prev_throttle, ts, prev_ts))
        prev_throttle, prev_ts = throttle, ts
    return d if d["t"] else {}


def load_thermal(conn, since, until, node=None):
    """{zone_name: {t: [...], temp: [...]}} from thermal_zones."""
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, zone, temp in _q(conn, "SELECT ts, zone, temp_c FROM thermal_zones "
                             "WHERE ts BETWEEN ? AND ?" + nf
                             + " ORDER BY zone, ts", [since, until, *np_]):
        z = data.setdefault(zone, {"t": [], "temp": []})
        z["t"].append(ts); z["temp"].append(temp)
    return data


def load_power(conn, since, until, node=None):
    """{rail_name: {t: [...], watts: [...], volts: [...], amps: [...]}} from power_samples."""
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, rail, watts, volts, amps in _q(conn,
            "SELECT ts, rail, watts, volts, amps FROM power_samples WHERE ts BETWEEN ? AND ?" + nf
            + " ORDER BY rail, ts", [since, until, *np_]):
        r = data.setdefault(rail, {"t": [], "watts": [], "volts": [], "amps": []})
        r["t"].append(ts); r["watts"].append(watts); r["volts"].append(volts); r["amps"].append(amps)
    return data


def load_tcp(conn, since, until, node=None):
    """TCP/UDP kernel counters as per-second deltas + conntrack table fill percentage."""
    nf, np_ = _filt(node)
    rows = _q(conn, "SELECT ts, retrans_segs, out_rsts, estab_resets, udp_in_errors, "
              "udp_no_ports, conntrack_used, conntrack_max FROM tcp_samples "
              "WHERE ts BETWEEN ? AND ?" + nf + " ORDER BY ts", [since, until, *np_])
    if not rows:
        return {}
    d: dict = {"t": [], "retrans": [], "out_rsts": [], "estab_resets": [],
               "udp_err": [], "udp_noport": [], "conntrack_pct": []}
    prev = {"retrans": None, "out_rsts": None, "estab_resets": None,
            "udp_err": None, "udp_noport": None}
    prev_ts = None
    for ts, retr, rst, est, uerr, unop, ct_used, ct_max in rows:
        d["t"].append(ts)
        d["retrans"].append(_rate(retr, prev["retrans"], ts, prev_ts))
        d["out_rsts"].append(_rate(rst, prev["out_rsts"], ts, prev_ts))
        d["estab_resets"].append(_rate(est, prev["estab_resets"], ts, prev_ts))
        d["udp_err"].append(_rate(uerr, prev["udp_err"], ts, prev_ts))
        d["udp_noport"].append(_rate(unop, prev["udp_noport"], ts, prev_ts))
        d["conntrack_pct"].append(round(100.0 * ct_used / ct_max, 2)
                                  if ct_used is not None and ct_max else None)
        prev.update(retrans=retr, out_rsts=rst, estab_resets=est, udp_err=uerr, udp_noport=unop)
        prev_ts = ts
    return d


def load_disk_health(conn, since, until, node=None):
    """{device: {t: [...], wear: [...], ioerr: [...]}} from disk_health (hourly)."""
    nf, np_ = _filt(node)
    data: dict[str, dict] = {}
    for ts, dev, wear, ioerr in _q(conn,
            "SELECT ts, device, wear_pct, ioerr_count FROM disk_health WHERE ts BETWEEN ? AND ?" + nf
            + " ORDER BY device, ts", [since, until, *np_]):
        d = data.setdefault(dev, {"t": [], "wear": [], "ioerr": []})
        d["t"].append(ts); d["wear"].append(wear); d["ioerr"].append(ioerr)
    return data


# ---------- QW4: death clocks (linear extrapolation) ----------


def linear_eta_seconds(t, vals, target: float) -> float | None:
    """Seconds until a least-squares line through (t, vals) reaches `target`, or None
    when it cannot be projected: fewer than 3 finite points, a flat/zero time span, or
    a slope that is not moving toward the target (so nothing is filling up / wearing
    out). Already at/past target returns 0.0. Pure stdlib, hub-side at render time."""
    pts = [(ti, v) for ti, v in zip(t, vals) if ti is not None and v is not None and v == v]
    if len(pts) < 3:
        return None
    n = len(pts)
    mt = sum(p[0] for p in pts) / n
    mv = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mt) ** 2 for p in pts)
    if sxx == 0:
        return None
    slope = sum((p[0] - mt) * (p[1] - mv) for p in pts) / sxx  # units per second
    cur = pts[-1][1]
    if cur >= target:
        return 0.0
    if slope <= 0:  # not trending toward the target -> no finite ETA
        return None
    return (target - cur) / slope


def human_eta(seconds: float | None) -> str:
    """Compact countdown label: '~6h' / '~14d' / '~3y' / 'now'. None -> '' so callers
    can drop the annotation when nothing is projected."""
    if seconds is None:
        return ""
    if seconds <= 0:
        return "now"
    minutes = seconds / 60.0
    hours = minutes / 60.0
    days = hours / 24.0
    years = days / 365.0
    if years >= 1:
        return f"~{years:.0f}y"
    if days >= 1:
        return f"~{days:.0f}d"
    if hours >= 1:
        return f"~{hours:.0f}h"
    return f"~{minutes:.0f}m"


def _soonest_eta(series: dict, value_key: str, target: float) -> tuple[str, float] | None:
    """(name, seconds) for the series member projected to hit `target` first, or None."""
    best = None
    for name, d in series.items():
        eta = linear_eta_seconds(d.get("t", []), d.get(value_key, []), target)
        if eta is not None and (best is None or eta < best[1]):
            best = (name, eta)
    return best


def disk_full_eta(disk_data: dict) -> tuple[str, float] | None:
    """(mount, seconds) for the mount filling toward 100% used soonest, or None."""
    return _soonest_eta(disk_data, "used", 100.0)


def wear_eta(health_data: dict) -> tuple[str, float] | None:
    """(device, seconds) for the SD/eMMC reaching 100% wear soonest, or None."""
    return _soonest_eta(health_data, "wear", 100.0)


def load_self(conn, since, until, node=None):
    """S5: smokemon's own rss/cpu over time, from the proc_samples rows the host probe
    records for itself (name='smokemon'). Proves the low-RSS claim - the monitor shows
    up in its own data."""
    nf, np_ = _filt(node)
    d: dict = {"t": [], "rss": [], "cpu": []}
    for ts, cpu, rss in _q(conn, "SELECT ts, cpu_pct, rss_mb FROM proc_samples "
                           "WHERE name='smokemon' AND ts BETWEEN ? AND ?" + nf + " ORDER BY ts",
                           [since, until, *np_]):
        d["t"].append(ts); d["cpu"].append(cpu); d["rss"].append(rss)
    return d if d["t"] else {}


def load_all(conn, since, until, targets, node, sel, ping_loader):
    """Load every selected panel's series in one place so the TUI and PNG renderers
    cannot drift apart when a panel is added. The only per-renderer difference is the
    ping loader: TUI uses load_ping_agg (min/median/max), PNG uses load_ping_smoke
    (percentile bands), passed in as ping_loader."""
    return {
        "ping":    ping_loader(conn, since, until, targets, node) if "ping" in sel else {},
        "net":     load_net(conn, since, until, node) if "net" in sel else {},
        "http":    load_http(conn, since, until, node) if "http" in sel else {},
        "mtr":     load_mtr(conn, since, until, node) if "mtr" in sel else {},
        "wifi":    load_wifi(conn, since, until, node) if "wifi" in sel else {},
        "iperf":   load_iperf(conn, since, until, node) if "iperf" in sel else {},
        "host":    load_host(conn, since, until, node) if "host" in sel else {},
        "disk":    load_disk(conn, since, until, node) if "disk" in sel else {},
        # Not a panel of its own; loaded with disk so the disk death-clock can show SD wear.
        "disk_health": load_disk_health(conn, since, until, node) if "disk" in sel else {},
        "thermal": load_thermal(conn, since, until, node) if "thermal" in sel else {},
        "power":   load_power(conn, since, until, node) if "power" in sel else {},
        "tcp":     load_tcp(conn, since, until, node) if "tcp" in sel else {},
        "psi":     load_psi(conn, since, until, node) if "psi" in sel else {},
        "freq":    load_freq(conn, since, until, node) if "freq" in sel else {},
        "self":    load_self(conn, since, until, node) if "self" in sel else {},
    }
