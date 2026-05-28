#!/usr/bin/env python3
"""smokemon plotter: render a granular PNG of latency (smokeping-style smoke), loss,
bandwidth, HTTP timing, mtr per-hop, WiFi signal and iperf throughput."""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")
DEFAULT_OUT = os.path.join(HOME, "smokemon", "graphs", "smokemon.png")

SKIP_IFACE_PREFIXES = ("lo", "gif", "stf", "anpi", "bridge", "ap")
TARGET_LABELS = {"1.1.1.1": "internet", "100.127.203.7": "vpn", "192.168.0.1": "gw"}
ALL_PANELS = ["ping", "net", "http", "mtr", "wifi", "iperf", "host", "disk"]


def parse_args():
    p = argparse.ArgumentParser(description="Plot smokemon data")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--hours", type=float, default=6.0, help="how far back (default 6h)")
    p.add_argument("--minutes", type=float, help="window in minutes, overrides --hours")
    p.add_argument("--since", help="ISO time, overrides --hours/--minutes")
    p.add_argument("--until", help="ISO time (default now)")
    p.add_argument("--targets", help="comma-separated list to limit to")
    p.add_argument("--node", help="limit to a single node (required when plotting a hub DB)")
    p.add_argument("--panels", default="all", help=f"panels: {','.join(ALL_PANELS)} or 'all'")
    p.add_argument("--dpi", type=int, default=96, help="PNG resolution (granularity comes mostly from width)")
    p.add_argument("--width", type=float, default=0, help="figure width in inches (0 = auto from time span)")
    p.add_argument("--no-open", action="store_true", help="do not open the PNG afterwards")
    return p.parse_args()


def time_window(args) -> tuple[float, float]:
    until = datetime.fromisoformat(args.until).timestamp() if args.until else datetime.now().timestamp()
    if args.since:
        since = datetime.fromisoformat(args.since).timestamp()
    elif args.minutes is not None:
        since = until - args.minutes * 60
    else:
        since = until - args.hours * 3600
    return since, until


def _q(conn, sql, params):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _dt(ts):
    return datetime.fromtimestamp(ts)


def _node_clause(node, params):
    """Append a node filter when --node is given (hub DBs hold many nodes)."""
    if node:
        params.append(node)
        return " AND node=?"
    return ""


def load_ping(conn, since, until, targets, node=None):
    q = "SELECT id, ts, target, loss_pct FROM ping_runs WHERE ts BETWEEN ? AND ?"
    params = [since, until]
    if targets:
        q += " AND target IN (%s)" % ",".join("?" * len(targets))
        params += targets
    q += _node_clause(node, params)
    q += " ORDER BY ts"
    runs = _q(conn, q, params)
    if not runs:
        return {}
    rtts: dict[int, list[float]] = {r[0]: [] for r in runs}
    run_ids = list(rtts)
    for i in range(0, len(run_ids), 800):  # batch around SQLite's variable limit
        chunk = run_ids[i:i + 800]
        for rid, rtt in conn.execute(
            "SELECT run_id, rtt_ms FROM ping_rtts WHERE run_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            rtts[rid].append(rtt)
    by_target: dict[str, dict] = {}
    for rid, ts, target, loss in runs:
        d = by_target.setdefault(target, {"t": [], "p0": [], "p25": [], "p50": [], "p75": [], "p100": [], "loss": []})
        d["t"].append(_dt(ts)); d["loss"].append(loss)
        samples = rtts.get(rid)
        if samples:
            arr = np.array(samples)
            for k, pc in (("p0", 0), ("p25", 25), ("p50", 50), ("p75", 75), ("p100", 100)):
                d[k].append(np.percentile(arr, pc))
        else:
            for k in ("p0", "p25", "p50", "p75", "p100"):
                d[k].append(np.nan)
    return by_target


def load_net(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    rows = _q(conn, f"SELECT ts, iface, ibytes, obytes FROM net_samples WHERE ts BETWEEN ? AND ?{nc} ORDER BY iface, ts",
              params)
    series, prev = {}, {}
    for ts, iface, ib, ob in rows:
        if iface.startswith(SKIP_IFACE_PREFIXES):
            continue
        if iface in prev:
            pts, pib, pob = prev[iface]
            dt = ts - pts
            din, dout = ib - pib, ob - pob
            if dt > 0 and din >= 0 and dout >= 0:
                s = series.setdefault(iface, {"t": [], "in": [], "out": []})
                s["t"].append(_dt(ts)); s["in"].append(din * 8 / 1e6 / dt); s["out"].append(dout * 8 / 1e6 / dt)
        prev[iface] = (ts, ib, ob)
    return {i: s for i, s in series.items() if s["in"] and (max(s["in"]) > 0.01 or max(s["out"]) > 0.01)}


def load_http(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    data = {}
    for ts, url, ttfb in _q(
        conn, f"SELECT ts,url,ttfb_ms FROM http_samples WHERE ts BETWEEN ? AND ?{nc} ORDER BY url, ts", params):
        d = data.setdefault(url, {"t": [], "ttfb": []})
        d["t"].append(_dt(ts)); d["ttfb"].append(ttfb)
    return data


def load_mtr(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    data = {}
    for ts, target, hop, host, loss, avg in _q(
        conn, f"SELECT ts,target,hop_no,host,loss_pct,avg_ms FROM mtr_hops WHERE ts BETWEEN ? AND ?{nc} ORDER BY ts,hop_no",
        params):
        h = data.setdefault(target, {}).setdefault(hop, {"t": [], "avg": [], "host": host, "loss": []})
        h["t"].append(_dt(ts)); h["avg"].append(avg); h["loss"].append(loss); h["host"] = host
    return data


def load_wifi(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    d = {"t": [], "rssi": [], "noise": [], "tx": []}
    for ts, rssi, noise, tx in _q(
        conn, f"SELECT ts,rssi_dbm,noise_dbm,tx_rate_mbps FROM wifi_samples WHERE ts BETWEEN ? AND ?{nc} ORDER BY ts",
        params):
        d["t"].append(_dt(ts)); d["rssi"].append(rssi); d["noise"].append(noise); d["tx"].append(tx)
    return d if d["t"] else {}


def load_iperf(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    d = {"t": [], "up": [], "down": []}
    for ts, up, down in _q(
        conn, f"SELECT ts,up_mbps,down_mbps FROM iperf_samples WHERE ts BETWEEN ? AND ?{nc} ORDER BY ts", params):
        d["t"].append(_dt(ts)); d["up"].append(up); d["down"].append(down)
    return d if d["t"] else {}


def load_host(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    d = {"t": [], "cpu": [], "mem": [], "temp": [], "rd": [], "wr": []}
    for ts, cpu, mem, temp, rd, wr in _q(
        conn, "SELECT ts,cpu_pct,mem_used_pct,temp_c,disk_read_mbps,disk_write_mbps "
        f"FROM host_samples WHERE ts BETWEEN ? AND ?{nc} ORDER BY ts", params):
        d["t"].append(_dt(ts)); d["cpu"].append(cpu); d["mem"].append(mem)
        d["temp"].append(temp); d["rd"].append(rd); d["wr"].append(wr)
    return d if d["t"] else {}


def load_disk(conn, since, until, node=None):
    params = [since, until]
    nc = _node_clause(node, params)
    data = {}
    for ts, mount, used, free in _q(
        conn, f"SELECT ts,mount,used_pct,free_gb FROM disk_samples WHERE ts BETWEEN ? AND ?{nc} ORDER BY mount, ts",
        params):
        m = data.setdefault(mount, {"t": [], "used": [], "free": []})
        m["t"].append(_dt(ts)); m["used"].append(used); m["free"].append(free)
    return data


def host_label(url):
    h = urlparse(url).netloc.replace("www.", "")
    return h.rsplit(".", 1)[0] if "." in h else (h or url)  # strip domain suffix (.com etc.)


def build_panels(selected, ping, net, http, mtr, wifi, iperf, host, disk):
    panels = []

    if "ping" in selected:
        for target, d in sorted(ping.items()):
            def draw(ax, d=d, target=target):
                t = d["t"]
                ax.fill_between(t, d["p0"], d["p100"], color="#3b6ea5", alpha=0.18, label="min–max")
                ax.fill_between(t, d["p25"], d["p75"], color="#3b6ea5", alpha=0.35, label="p25–p75")
                ax.plot(t, d["p50"], color="#15324f", lw=1.0, label="median")
                loss = np.array(d["loss"]); lossy = loss > 0
                if lossy.any():
                    ax.scatter(np.array(t)[lossy], np.array(d["p50"])[lossy], c=loss[lossy], cmap="autumn_r",
                               vmin=0, vmax=100, s=18, zorder=5, edgecolors="k", linewidths=0.3, label="loss")
                avg_loss = float(np.nanmean(loss)) if len(loss) else 0.0
                label = TARGET_LABELS.get(target, target)
                ax.set_title(f"{label} ({target})   avg loss {avg_loss:.1f}%", loc="left", fontsize=10, fontweight="bold")
                ax.set_ylabel("RTT (ms)"); ax.set_ylim(bottom=0)
            panels.append(draw)

    if "net" in selected and net:
        def draw_net(ax, net=net):
            for iface, s in sorted(net.items()):
                ax.plot(s["t"], s["in"], lw=0.9, label=f"{iface} down")
                ax.plot(s["t"], s["out"], lw=0.9, ls="--", label=f"{iface} up")
            ax.set_title("Bandwidth per interface (Mbit/s)", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(draw_net)

    if "http" in selected and http:
        def draw_http(ax, http=http):
            for url, d in sorted(http.items()):
                ax.plot(d["t"], d["ttfb"], lw=0.9, label=host_label(url))
            ax.set_title("HTTP TTFB (ms) — time to first byte", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("ms"); ax.set_ylim(bottom=0)
        panels.append(draw_http)

    if "mtr" in selected:
        for target, hops in sorted(mtr.items()):
            def draw_mtr(ax, hops=hops, target=target):
                worst = 0.0
                for hop_no in sorted(hops):
                    h = hops[hop_no]
                    lbl = f"h{hop_no} {h['host']}" if h.get("host") else f"h{hop_no}"
                    ax.plot(h["t"], h["avg"], lw=0.9, label=lbl)
                    if h["loss"]:
                        worst = max(worst, max(h["loss"]))
                ax.set_title(f"mtr per-hop → {target}   (worst hop-loss {worst:.0f}%)", loc="left", fontsize=10, fontweight="bold")
                ax.set_ylabel("avg ms/hop"); ax.set_ylim(bottom=0)
            panels.append(draw_mtr)

    if "wifi" in selected and wifi:
        def draw_wifi(ax, wifi=wifi):
            ax.plot(wifi["t"], wifi["rssi"], color="#2ca02c", lw=0.9, label="RSSI dBm")
            ax.plot(wifi["t"], wifi["noise"], color="#888888", lw=0.9, label="noise dBm")
            tx = next((v for v in reversed(wifi["tx"]) if v is not None), None)
            extra = f"tx {tx:.0f} Mbit/s" if tx else ""
            ax.set_title(f"WiFi signal (dBm, higher=better)   {extra}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("dBm")
        panels.append(draw_wifi)

    if "iperf" in selected and iperf:
        def draw_iperf(ax, iperf=iperf):
            ax.plot(iperf["t"], iperf["down"], lw=1.0, marker="o", ms=3, label="down")
            ax.plot(iperf["t"], iperf["up"], lw=1.0, marker="o", ms=3, label="up")
            ax.set_title("iperf3 throughput (Mbit/s) — active test", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(draw_iperf)

    if "host" in selected and host:
        def draw_host(ax, d=host):
            ax.plot(d["t"], d["cpu"], color="#d62728", lw=0.9, label="CPU %")
            ax.plot(d["t"], d["mem"], color="#1f77b4", lw=0.9, label="Mem %")
            if any(v is not None for v in d["temp"]):
                ax.plot(d["t"], d["temp"], color="#ff7f0e", lw=0.9, ls="--", label="Temp °C")
            tnow = next((v for v in reversed(d["temp"]) if v is not None), None)
            rd = next((v for v in reversed(d["rd"]) if v is not None), None)
            wr = next((v for v in reversed(d["wr"]) if v is not None), None)
            extra = []
            if tnow is not None:
                extra.append(f"temp {tnow:.0f}°C")
            if rd is not None or wr is not None:
                extra.append(f"disk r/w {rd or 0:.1f}/{wr or 0:.1f} MB/s")
            tail = "   " + " · ".join(extra) if extra else ""
            ax.set_title(f"Host — CPU/mem (%) + temp{tail}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("% / °C"); ax.set_ylim(bottom=0)
        panels.append(draw_host)

    if "disk" in selected and disk:
        def draw_disk(ax, data=disk):
            for mount, m in sorted(data.items()):
                free = next((v for v in reversed(m["free"]) if v is not None), None)
                lbl = f"{mount} ({free:.0f} GB free)" if free is not None else mount
                ax.plot(m["t"], m["used"], lw=0.9, label=lbl)
            ax.set_title("Disk usage per mount (%)", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("% used"); ax.set_ylim(0, 100)
        panels.append(draw_disk)

    return panels


def plot(panels, args, since, until):
    if not panels:
        print("No data in selected time window.", file=sys.stderr)
        return False
    span_h = (until - since) / 3600
    # Width drives granularity: ~2 in/hour so each 10s sample stays distinguishable.
    width = args.width if args.width > 0 else min(80.0, max(16.0, span_h * 2))
    fig, axes = plt.subplots(len(panels), 1, figsize=(width, 3.0 * len(panels)), sharex=True, squeeze=False)
    axes = axes[:, 0]
    for ax, draw in zip(axes, panels):
        draw(ax)
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left", fontsize=7, ncol=5, framealpha=0.8)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))  # no y decimals
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))  # no seconds
    fig.autofmt_xdate()
    node_tag = f"[{args.node}] " if args.node else ""
    fig.suptitle(
        f"smokemon {node_tag}— {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → "
        f"{datetime.fromtimestamp(until):%H:%M}  ({span_h:.1f}h)", fontsize=12, y=0.997)
    fig.tight_layout(rect=(0, 0, 1, 0.99))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fig.savefig(args.out, dpi=args.dpi)
    print(f"Saved graph: {args.out}")
    return True


def main() -> int:
    args = parse_args()
    if not os.path.exists(args.db):
        print(f"No database found: {args.db}", file=sys.stderr)
        return 1
    since, until = time_window(args)
    targets = [t.strip() for t in args.targets.split(",")] if args.targets else None
    selected = ALL_PANELS if args.panels.strip() == "all" else [s.strip() for s in args.panels.split(",")]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    node = args.node
    ping = load_ping(conn, since, until, targets, node) if "ping" in selected else {}
    net = load_net(conn, since, until, node) if "net" in selected else {}
    http = load_http(conn, since, until, node) if "http" in selected else {}
    mtr = load_mtr(conn, since, until, node) if "mtr" in selected else {}
    wifi = load_wifi(conn, since, until, node) if "wifi" in selected else {}
    iperf = load_iperf(conn, since, until, node) if "iperf" in selected else {}
    host = load_host(conn, since, until, node) if "host" in selected else {}
    disk = load_disk(conn, since, until, node) if "disk" in selected else {}
    conn.close()

    panels = build_panels(selected, ping, net, http, mtr, wifi, iperf, host, disk)
    ok = plot(panels, args, since, until)
    if ok and not args.no_open:
        subprocess.run(["/usr/bin/open", args.out], check=False)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
