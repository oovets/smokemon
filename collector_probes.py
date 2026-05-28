#!/usr/bin/env python3
"""smokemon probe collector: heavier/slower measurements on their own cadence
(default 60s), separate from the fast ping/net loop. Stdlib only.
  - HTTP/S timing (curl HEAD): DNS, TCP connect, TLS, TTFB, total per URL
  - mtr per-hop (sudo -n mtr --json): latency/loss per hop along the path
  - WiFi signal (system_profiler): RSSI, noise, tx-rate, channel
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import platform_adapters as pa

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")

DB_PATH = os.environ.get("SMOKEMON_DB", DEFAULT_DB)
INTERVAL = float(os.environ.get("SMOKEMON_PROBE_INTERVAL", "60"))
HTTP_URLS = [u.strip() for u in os.environ.get(
    "SMOKEMON_HTTP_URLS", "https://www.google.com,https://www.cloudflare.com").split(",") if u.strip()]
MTR_TARGETS = [t.strip() for t in os.environ.get("SMOKEMON_MTR_TARGETS", "1.1.1.1").split(",") if t.strip()]
CURL = pa.cli_path("SMOKEMON_CURL", "curl")
MTR = pa.cli_path("SMOKEMON_MTR", "mtr")
MTR_COUNT = int(os.environ.get("SMOKEMON_MTR_COUNT", "10"))
# mtr behöver root: macOS via lösenordslös sudo; Linux kan istället setcap:a binären
# (install_linux.sh) och sätta SMOKEMON_MTR_SUDO=0 för att slippa sudo.
MTR_SUDO = os.environ.get("SMOKEMON_MTR_SUDO", "1") != "0"
WIFI_ENABLED = os.environ.get("SMOKEMON_WIFI", "1") != "0"
NODE = pa.NODE

_running = True


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _stop(signum, _frame):
    global _running
    _running = False
    log(f"signal {signum} received, exiting after current cycle")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS http_samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            url TEXT NOT NULL,
            http_code INTEGER,
            dns_ms REAL, connect_ms REAL, tls_ms REAL, ttfb_ms REAL, total_ms REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_http_ts ON http_samples(ts);
        CREATE INDEX IF NOT EXISTS ix_http_url_ts ON http_samples(url, ts);

        CREATE TABLE IF NOT EXISTS mtr_hops (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            target TEXT NOT NULL,
            hop_no INTEGER NOT NULL,
            host TEXT,
            loss_pct REAL, sent INTEGER,
            last_ms REAL, avg_ms REAL, best_ms REAL, worst_ms REAL, stddev_ms REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_mtr_ts ON mtr_hops(ts);
        CREATE INDEX IF NOT EXISTS ix_mtr_target_ts ON mtr_hops(target, ts);

        CREATE TABLE IF NOT EXISTS wifi_samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            ssid TEXT, channel TEXT, phy_mode TEXT,
            rssi_dbm INTEGER, noise_dbm INTEGER, tx_rate_mbps REAL,
            node TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_wifi_ts ON wifi_samples(ts);
        """
    )
    conn.commit()
    pa.ensure_node_column(conn, ("http_samples", "mtr_hops", "wifi_samples"))


# ---- HTTP -------------------------------------------------------------------

_CURL_FMT = ("code=%{http_code} dns=%{time_namelookup} conn=%{time_connect} "
             "tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}")


def http_probe(url: str) -> dict | None:
    """curl HEAD request, timings returned in ms (HEAD avoids downloading the body)."""
    try:
        proc = subprocess.run(
            [CURL, "-sI", "-o", "/dev/null", "--max-time", "10", "-w", _CURL_FMT, url],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        log(f"curl error {url}: {e!r}")
        return None
    vals = dict(tok.split("=", 1) for tok in proc.stdout.split() if "=" in tok)
    if "total" not in vals:
        return None
    return {
        "code": int(vals.get("code", 0)),
        "dns_ms": float(vals["dns"]) * 1000,
        "connect_ms": float(vals["conn"]) * 1000,
        "tls_ms": float(vals["tls"]) * 1000,
        "ttfb_ms": float(vals["ttfb"]) * 1000,
        "total_ms": float(vals["total"]) * 1000,
    }


# ---- mtr --------------------------------------------------------------------

def mtr_probe(target: str) -> list[dict]:
    """mtr --json -> list of hops. Needs root: passwordless sudo (macOS) or cap_net_raw
    on the binary with SMOKEMON_MTR_SUDO=0 (Linux)."""
    cmd = (["sudo", "-n"] if MTR_SUDO else []) + [MTR, "-n", "--json", "-c", str(MTR_COUNT), "-i", "0.2", target]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=MTR_COUNT * 0.2 + 30,
        )
    except Exception as e:  # noqa: BLE001
        log(f"mtr error {target}: {e!r}")
        return []
    try:
        hubs = json.loads(proc.stdout)["report"]["hubs"]
    except (ValueError, KeyError) as e:
        log(f"mtr parse error {target}: {e!r} (stderr: {proc.stderr[:120]})")
        return []
    return [
        {"hop_no": h.get("count"), "host": h.get("host"), "loss_pct": h.get("Loss%"),
         "sent": h.get("Snt"), "last_ms": h.get("Last"), "avg_ms": h.get("Avg"),
         "best_ms": h.get("Best"), "worst_ms": h.get("Wrst"), "stddev_ms": h.get("StDev")}
        for h in hubs
    ]


# ---- cycle ------------------------------------------------------------------

def cycle(conn: sqlite3.Connection) -> None:
    ts = time.time()
    for url in HTTP_URLS:
        r = http_probe(url)
        if r:
            conn.execute(
                "INSERT INTO http_samples (ts,url,http_code,dns_ms,connect_ms,tls_ms,ttfb_ms,total_ms,node)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, url, r["code"], r["dns_ms"], r["connect_ms"], r["tls_ms"], r["ttfb_ms"], r["total_ms"], NODE),
            )
    for target in MTR_TARGETS:
        for h in mtr_probe(target):
            conn.execute(
                "INSERT INTO mtr_hops (ts,target,hop_no,host,loss_pct,sent,last_ms,avg_ms,best_ms,worst_ms,stddev_ms,node)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, target, h["hop_no"], h["host"], h["loss_pct"], h["sent"],
                 h["last_ms"], h["avg_ms"], h["best_ms"], h["worst_ms"], h["stddev_ms"], NODE),
            )
    if WIFI_ENABLED:
        w = pa.wifi_probe()
        if w:
            conn.execute(
                "INSERT INTO wifi_samples (ts,ssid,channel,phy_mode,rssi_dbm,noise_dbm,tx_rate_mbps,node)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, w["ssid"], w["channel"], w["phy_mode"], w["rssi_dbm"], w["noise_dbm"], w["tx_rate_mbps"], NODE),
            )
    conn.commit()


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)
    log(f"start: node={NODE} interval={INTERVAL}s http={HTTP_URLS} mtr={MTR_TARGETS} wifi={WIFI_ENABLED} db={DB_PATH}")
    while _running:
        start = time.time()
        try:
            cycle(conn)
        except Exception as e:  # noqa: BLE001
            log(f"cycle error: {e!r}")
        next_t = (int(start // INTERVAL) + 1) * INTERVAL
        sleep = next_t - time.time()
        while sleep > 0 and _running:
            time.sleep(min(sleep, 1.0))
            sleep = next_t - time.time()
    conn.close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
