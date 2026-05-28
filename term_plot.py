#!/usr/bin/env python3
"""smokemon TUI plot: render latency/loss/bandwidth/HTTP/mtr/WiFi/iperf as text in
the terminal (plotext, braille). No graphics, just characters."""

import argparse
import math
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from urllib.parse import urlparse

import plotext as plt

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "smokemon", "data", "smokemon.db")
SKIP_IFACE_PREFIXES = ("lo", "gif", "stf", "anpi", "bridge", "ap")

TARGET_LABELS = {"1.1.1.1": "internet", "100.127.203.7": "vpn", "192.168.0.1": "gw"}
HTTP_COLORS = ["cyan", "green+", "magenta+", "blue+", "orange+"]  # distinct, non-red

ALL_PANELS = ["ping", "net", "http", "mtr", "wifi", "iperf"]

KIOSK = False  # clean graphs (no legend/axes/ticks/title); set in main()


def L(s):
    """Label -> None in kiosk mode (suppresses the legend entry)."""
    return None if KIOSK else s


def _ylabel(s):
    if not KIOSK:
        plt.ylabel(s)


def _title(s):
    if not KIOSK:
        plt.title(s)


def parse_args():
    p = argparse.ArgumentParser(description="smokemon TUI plot")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--minutes", type=float, help="window in minutes, overrides --hours")
    p.add_argument("--since", help="ISO time")
    p.add_argument("--until", help="ISO time")
    p.add_argument("--targets", help="comma-separated ping targets")
    p.add_argument("--panels", default=",".join(ALL_PANELS),
                   help=f"panels: {','.join(ALL_PANELS)} or 'all'")
    p.add_argument("--reserve", type=int, default=0, help="rows to leave above the plot (live header)")
    p.add_argument("--kiosk", action="store_true", help="clean graphs: no legend, axes, ticks or title")
    return p.parse_args()


def time_window(args):
    until = datetime.fromisoformat(args.until).timestamp() if args.until else datetime.now().timestamp()
    if args.since:
        since = datetime.fromisoformat(args.since).timestamp()
    elif args.minutes is not None:
        since = until - args.minutes * 60
    else:
        since = until - args.hours * 3600
    return since, until


def _q(conn, sql, params):
    """Run query; return [] if the table doesn't exist yet."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def load_ping(conn, since, until, targets):
    q = "SELECT ts, target, loss_pct, rtt_min, rtt_median, rtt_max FROM ping_runs WHERE ts BETWEEN ? AND ?"
    params = [since, until]
    if targets:
        q += " AND target IN (%s)" % ",".join("?" * len(targets))
        params += targets
    q += " ORDER BY target, ts"
    data = {}
    for ts, target, loss, rmin, rmed, rmax in _q(conn, q, params):
        d = data.setdefault(target, {"t": [], "min": [], "med": [], "max": [], "loss": []})
        d["t"].append(ts); d["min"].append(rmin); d["med"].append(rmed)
        d["max"].append(rmax); d["loss"].append(loss)
    return data


def load_net(conn, since, until):
    rows = _q(conn, "SELECT ts, iface, ibytes, obytes FROM net_samples WHERE ts BETWEEN ? AND ? ORDER BY iface, ts",
              (since, until))
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
                s["t"].append(ts); s["in"].append(din * 8 / 1e6 / dt); s["out"].append(dout * 8 / 1e6 / dt)
        prev[iface] = (ts, ib, ob)
    return {i: s for i, s in series.items() if s["in"] and (max(s["in"]) > 0.01 or max(s["out"]) > 0.01)}


def load_http(conn, since, until):
    data = {}
    for ts, url, ttfb in _q(
        conn, "SELECT ts,url,ttfb_ms FROM http_samples WHERE ts BETWEEN ? AND ? ORDER BY url, ts", (since, until)):
        d = data.setdefault(url, {"t": [], "ttfb": []})
        d["t"].append(ts); d["ttfb"].append(ttfb)
    return data


def load_mtr(conn, since, until):
    data = {}  # target -> hop_no -> {t, avg, host, loss}
    for ts, target, hop, host, loss, avg in _q(
        conn, "SELECT ts,target,hop_no,host,loss_pct,avg_ms FROM mtr_hops WHERE ts BETWEEN ? AND ? ORDER BY ts,hop_no",
        (since, until)):
        h = data.setdefault(target, {}).setdefault(hop, {"t": [], "avg": [], "host": host, "loss": []})
        h["t"].append(ts); h["avg"].append(avg); h["loss"].append(loss); h["host"] = host
    return data


def load_wifi(conn, since, until):
    d = {"t": [], "rssi": [], "noise": [], "tx": []}
    for ts, rssi, noise, tx in _q(
        conn, "SELECT ts,rssi_dbm,noise_dbm,tx_rate_mbps FROM wifi_samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (since, until)):
        d["t"].append(ts); d["rssi"].append(rssi); d["noise"].append(noise); d["tx"].append(tx)
    return d if d["t"] else {}


def load_iperf(conn, since, until):
    d = {"t": [], "up": [], "down": []}
    for ts, up, down in _q(
        conn, "SELECT ts,up_mbps,down_mbps FROM iperf_samples WHERE ts BETWEEN ? AND ? ORDER BY ts", (since, until)):
        d["t"].append(ts); d["up"].append(up); d["down"].append(down)
    return d if d["t"] else {}


def make_ticks(since, until):
    span = until - since
    fmt = "%H:%M" if span <= 86400 else "%m-%d %H:%M"  # never seconds
    ticks = [since + span * i / 6 for i in range(7)]
    return ticks, [datetime.fromtimestamp(t).strftime(fmt) for t in ticks]


def set_int_yticks(*value_lists):
    """Integer y-ticks (no decimals) for the current subplot."""
    if KIOSK:
        return
    vals = [v for lst in value_lists for v in lst if v is not None and v == v]
    if not vals:
        return
    lo, hi = min(vals), max(vals)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    step = max(1, round((hi - lo) / 5))
    ticks = list(range(math.floor(lo), math.ceil(hi) + 1, step))
    if len(ticks) >= 2:
        plt.yticks(ticks, [str(t) for t in ticks])


def host_label(url):
    h = urlparse(url).netloc.replace("www.", "")
    return h.rsplit(".", 1)[0] if "." in h else (h or url)  # strip domain suffix (.com etc.)


def build_panels(selected, ping, net, http, mtr, wifi, iperf):
    """Return draw fns in display order, only for panels that have data."""
    panels = []

    if "ping" in selected:
        for name, d in sorted(ping.items()):
            def draw(d=d, name=name):
                plt.plot(d["t"], d["max"], color=240, marker="braille")
                plt.plot(d["t"], d["min"], color=240, marker="braille")
                plt.plot(d["t"], d["med"], label=L("median"), color="orange+", marker="braille")
                lt = [t for t, l in zip(d["t"], d["loss"]) if l > 0]
                lm = [m for m, l in zip(d["med"], d["loss"]) if l > 0]
                if lt:
                    plt.scatter(lt, lm, color="red", marker="dot", label=L("loss"))
                avg_loss = sum(d["loss"]) / len(d["loss"]) if d["loss"] else 0.0
                cur = d["med"][-1] if d["med"] else float("nan")
                label = TARGET_LABELS.get(name, name)
                _title(f"{label} ({name})   median now {cur:.1f} ms · spread (gray) min–max · avg loss {avg_loss:.1f}%")
                _ylabel("RTT ms")
                set_int_yticks(d["min"], d["med"], d["max"])
            panels.append(draw)

    if "net" in selected and net:
        def draw_net(net=net):
            for iface, s in sorted(net.items()):
                plt.plot(s["t"], s["in"], label=L(f"{iface} down"), color="cyan", marker="braille")
                plt.plot(s["t"], s["out"], label=L(f"{iface} up"), color="orange", marker="braille")
            _title("Bandwidth (Mbit/s) — passive, actual traffic")
            _ylabel("Mbit/s")
            set_int_yticks(*[s["in"] for s in net.values()], *[s["out"] for s in net.values()])
        panels.append(draw_net)

    if "http" in selected and http:
        def draw_http(http=http):
            for i, (url, d) in enumerate(sorted(http.items())):
                plt.plot(d["t"], d["ttfb"], label=L(host_label(url)),
                         color=HTTP_COLORS[i % len(HTTP_COLORS)], marker="braille")
            _title("HTTP TTFB (ms) — time to first byte (DNS+TCP+TLS+server)")
            _ylabel("ms")
            set_int_yticks(*[d["ttfb"] for d in http.values()])
        panels.append(draw_http)

    if "mtr" in selected and mtr:
        for target, hops in sorted(mtr.items()):
            def draw_mtr(hops=hops, target=target):
                worst = 0.0
                for hop_no in sorted(hops):
                    h = hops[hop_no]
                    lbl = f"h{hop_no} {h['host']}" if h.get("host") else f"h{hop_no}"
                    plt.plot(h["t"], h["avg"], label=L(lbl), marker="braille")
                    if h["loss"]:
                        worst = max(worst, max(h["loss"]))
                _title(f"mtr per-hop → {target}   (avg latency/hop, worst hop-loss {worst:.0f}%)")
                _ylabel("ms")
                set_int_yticks(*[h["avg"] for h in hops.values()])
            panels.append(draw_mtr)

    if "wifi" in selected and wifi:
        def draw_wifi(wifi=wifi):
            plt.plot(wifi["t"], wifi["rssi"], label=L("RSSI dBm"), color="green+", marker="braille")
            plt.plot(wifi["t"], wifi["noise"], label=L("noise dBm"), color=240, marker="braille")
            tx = next((v for v in reversed(wifi["tx"]) if v is not None), None)
            snr = (wifi["rssi"][-1] - wifi["noise"][-1]) if wifi["rssi"] and wifi["noise"] else None
            extra = f"SNR {snr:.0f} dB" if snr is not None else ""
            extra += f" · tx {tx:.0f} Mbit/s" if tx else ""
            _title(f"WiFi signal (dBm, higher=better)   {extra}")
            _ylabel("dBm")
            set_int_yticks(wifi["rssi"], wifi["noise"])
        panels.append(draw_wifi)

    if "iperf" in selected and iperf:
        def draw_iperf(iperf=iperf):
            plt.plot(iperf["t"], iperf["down"], label=L("down"), color="cyan", marker="braille")
            plt.plot(iperf["t"], iperf["up"], label=L("up"), color="orange", marker="braille")
            _title("iperf3 throughput (Mbit/s) — active test to peer")
            _ylabel("Mbit/s")
            set_int_yticks(iperf["up"], iperf["down"])
        panels.append(draw_iperf)

    return panels


def main() -> int:
    global KIOSK
    args = parse_args()
    KIOSK = args.kiosk
    if not os.path.exists(args.db):
        print(f"No database found: {args.db}", file=sys.stderr)
        return 1
    since, until = time_window(args)
    targets = [t.strip() for t in args.targets.split(",")] if args.targets else None
    selected = ALL_PANELS if args.panels.strip() == "all" else [s.strip() for s in args.panels.split(",")]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ping = load_ping(conn, since, until, targets) if "ping" in selected else {}
    net = load_net(conn, since, until) if "net" in selected else {}
    http = load_http(conn, since, until) if "http" in selected else {}
    mtr = load_mtr(conn, since, until) if "mtr" in selected else {}
    wifi = load_wifi(conn, since, until) if "wifi" in selected else {}
    iperf = load_iperf(conn, since, until) if "iperf" in selected else {}
    conn.close()

    panels = build_panels(selected, ping, net, http, mtr, wifi, iperf)
    if not panels:
        print("No data in selected time window.", file=sys.stderr)
        return 2

    ticks, labels = make_ticks(since, until)
    plt.clf()
    plt.theme("pro")
    if args.reserve > 0:
        cols, lines = shutil.get_terminal_size(fallback=(120, 40))
        plt.plotsize(cols, max(10, lines - args.reserve))
    plt.subplots(len(panels), 1)
    for idx, draw in enumerate(panels, start=1):
        plt.subplot(idx, 1)
        plt.xlim(since, until)
        if KIOSK:
            plt.frame(True)        # keep the box around the graph
            plt.ticks_color(240)   # subtle dark-gray frame
            plt.xfrequency(0)      # no x ticks/labels
            plt.yfrequency(0)      # no y ticks/labels
        else:
            plt.xticks(ticks, labels)
        draw()
    plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
