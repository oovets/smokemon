"""PNG renderer (matplotlib). Width scales with the time span so each sample stays
distinguishable; dpi stays modest. Panels: ping/net/http/mtr/wifi/iperf/host/disk."""

import os
import sys
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from .. import config, query  # noqa: E402

ALL_PANELS = ["ping", "net", "http", "mtr", "wifi", "iperf", "host", "disk"]


def _dt(ts_list):
    return [datetime.fromtimestamp(t) for t in ts_list]


def _build(selected, data):
    panels = []
    if "ping" in selected:
        for target, d in sorted(data["ping"].items()):
            def draw(ax, d=d, target=target):
                t = _dt(d["t"])
                ax.fill_between(t, d["p0"], d["p100"], color="#3b6ea5", alpha=0.18, label="min–max")
                ax.fill_between(t, d["p25"], d["p75"], color="#3b6ea5", alpha=0.35, label="p25–p75")
                ax.plot(t, d["p50"], color="#15324f", lw=1.0, label="median")
                loss = np.array(d["loss"]); m = loss > 0
                if m.any():
                    ax.scatter(np.array(t)[m], np.array(d["p50"])[m], c=loss[m], cmap="autumn_r",
                               vmin=0, vmax=100, s=18, zorder=5, edgecolors="k", linewidths=0.3, label="loss")
                avg = float(np.nanmean(loss)) if len(loss) else 0.0
                ax.set_title(f"{config.TARGET_LABELS.get(target, target)} ({target})   avg loss {avg:.1f}%",
                             loc="left", fontsize=10, fontweight="bold")
                ax.set_ylabel("RTT (ms)"); ax.set_ylim(bottom=0)
            panels.append(draw)
    if "net" in selected and data["net"]:
        def draw_net(ax, net=data["net"]):
            for iface, s in sorted(net.items()):
                ax.plot(_dt(s["t"]), s["in"], lw=0.9, label=f"{iface} down")
                ax.plot(_dt(s["t"]), s["out"], lw=0.9, ls="--", label=f"{iface} up")
            ax.set_title("Bandwidth per interface (Mbit/s)", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(draw_net)
    if "http" in selected and data["http"]:
        def draw_http(ax, http=data["http"]):
            for url, d in sorted(http.items()):
                ax.plot(_dt(d["t"]), d["ttfb"], lw=0.9, label=query.host_label(url))
            ax.set_title("HTTP TTFB (ms) — time to first byte", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("ms"); ax.set_ylim(bottom=0)
        panels.append(draw_http)
    if "mtr" in selected:
        for target, hops in sorted(data["mtr"].items()):
            def draw_mtr(ax, hops=hops, target=target):
                worst = 0.0
                for hop_no in sorted(hops):
                    h = hops[hop_no]
                    ax.plot(_dt(h["t"]), h["avg"], lw=0.9,
                            label=f"h{hop_no} {h['host']}" if h.get("host") else f"h{hop_no}")
                    if h["loss"]:
                        worst = max(worst, max(h["loss"]))
                ax.set_title(f"mtr per-hop → {target}   (worst hop-loss {worst:.0f}%)",
                             loc="left", fontsize=10, fontweight="bold")
                ax.set_ylabel("avg ms/hop"); ax.set_ylim(bottom=0)
            panels.append(draw_mtr)
    if "wifi" in selected and data["wifi"]:
        def draw_wifi(ax, w=data["wifi"]):
            ax.plot(_dt(w["t"]), w["rssi"], color="#2ca02c", lw=0.9, label="RSSI dBm")
            ax.plot(_dt(w["t"]), w["noise"], color="#888888", lw=0.9, label="noise dBm")
            tx = next((v for v in reversed(w["tx"]) if v is not None), None)
            ax.set_title(f"WiFi signal (dBm, higher=better)   {f'tx {tx:.0f} Mbit/s' if tx else ''}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("dBm")
        panels.append(draw_wifi)
    if "iperf" in selected and data["iperf"]:
        def draw_iperf(ax, d=data["iperf"]):
            ax.plot(_dt(d["t"]), d["down"], lw=1.0, marker="o", ms=3, label="down")
            ax.plot(_dt(d["t"]), d["up"], lw=1.0, marker="o", ms=3, label="up")
            ax.set_title("iperf3 throughput (Mbit/s) — active test", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(draw_iperf)
    if "host" in selected and data["host"]:
        def draw_host(ax, d=data["host"]):
            ax.plot(_dt(d["t"]), d["cpu"], color="#ff7f0e", lw=0.9, label="cpu %")
            ax.plot(_dt(d["t"]), d["mem"], color="#1f77b4", lw=0.9, label="mem %")
            temp = next((v for v in reversed(d["temp"]) if v is not None), None)
            ax.set_title(f"Host cpu/mem (%)   {f'temp {temp:.0f}C' if temp is not None else ''}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("%"); ax.set_ylim(bottom=0)
        panels.append(draw_host)
    if "disk" in selected and data["disk"]:
        def draw_disk(ax, disk=data["disk"]):
            for mount, d in sorted(disk.items()):
                ax.plot(_dt(d["t"]), d["used"], lw=0.9, label=mount)
            ax.set_title("Disk used (%) per mount", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("%"); ax.set_ylim(bottom=0)
        panels.append(draw_disk)
    return panels


def run(opts) -> int:
    if not os.path.exists(opts.db):
        print(f"No database found: {opts.db}", file=sys.stderr)
        return 1
    since, until = query.window(opts.hours, opts.minutes, opts.since, opts.until)
    targets = [t.strip() for t in opts.targets.split(",")] if opts.targets else None
    sel = ALL_PANELS if opts.panels == "all" else [s.strip() for s in opts.panels.split(",")]
    node = opts.node
    conn = query.open_ro(opts.db)
    data = {
        "ping": query.load_ping_smoke(conn, since, until, targets, node) if "ping" in sel else {},
        "net": query.load_net(conn, since, until, node) if "net" in sel else {},
        "http": query.load_http(conn, since, until, node) if "http" in sel else {},
        "mtr": query.load_mtr(conn, since, until, node) if "mtr" in sel else {},
        "wifi": query.load_wifi(conn, since, until, node) if "wifi" in sel else {},
        "iperf": query.load_iperf(conn, since, until, node) if "iperf" in sel else {},
        "host": query.load_host(conn, since, until, node) if "host" in sel else {},
        "disk": query.load_disk(conn, since, until, node) if "disk" in sel else {},
    }
    conn.close()
    panels = _build(sel, data)
    if not panels:
        print("No data in selected time window.", file=sys.stderr)
        return 2
    span_h = (until - since) / 3600
    width = opts.width if opts.width > 0 else min(80.0, max(16.0, span_h * 2))  # width drives granularity
    fig, axes = plt.subplots(len(panels), 1, figsize=(width, 3.0 * len(panels)), sharex=True, squeeze=False)
    for ax, draw in zip(axes[:, 0], panels):
        draw(ax)
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left", fontsize=7, ncol=5, framealpha=0.8)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))  # no y decimals
    axes[-1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))  # no seconds
    fig.autofmt_xdate()
    tag = f" [{node}]" if node else ""
    fig.suptitle(f"smokemon{tag} — {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → "
                 f"{datetime.fromtimestamp(until):%H:%M}  ({span_h:.1f}h)", fontsize=12, y=0.997)
    fig.tight_layout(rect=(0, 0, 1, 0.99))
    os.makedirs(os.path.dirname(opts.out), exist_ok=True)
    fig.savefig(opts.out, dpi=opts.dpi)
    print(f"Saved graph: {opts.out}")
    if not opts.no_open:
        __import__("subprocess").run(["/usr/bin/open", opts.out], check=False)
    return 0
