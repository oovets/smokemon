"""PNG renderer (matplotlib). Panels arranged on a configurable grid (default 2 cols);
each panel keeps a 'logical width' proportional to the time span so individual samples
stay distinguishable. Set --cols 1 for the classic single-column stack."""

import math
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

ALL_PANELS = config.PANELS

PI_BIT_LABELS = {
    0: "uv-now", 1: "freq-cap-now", 2: "throttled-now", 3: "soft-temp-now",
    16: "uv-since-boot", 17: "freq-cap-since-boot", 18: "throttled-since-boot", 19: "soft-temp-since-boot",
}


def _dt(ts_list):
    return [datetime.fromtimestamp(t) for t in ts_list]


def _pi_bits_summary(bits_list):
    """Return human-readable list of bits ever seen set across the window."""
    seen = 0
    for b in bits_list:
        if b is not None:
            seen |= int(b)
    return [name for bit, name in PI_BIT_LABELS.items() if seen & (1 << bit)]


def _build(selected, data):  # noqa: C901 -- straight-line dispatch, intentionally flat
    panels = []
    if "ping" in selected:
        for target, d in sorted(data["ping"].items()):
            def draw(ax, d=d, target=target):
                t = _dt(d["t"])
                ax.fill_between(t, d["p0"], d["p100"], color="#3b6ea5", alpha=0.18, label="min-max")
                ax.fill_between(t, d["p25"], d["p75"], color="#3b6ea5", alpha=0.35, label="p25-p75")
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
            ax.set_title("HTTP TTFB (ms) - time to first byte", loc="left", fontsize=10, fontweight="bold")
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
                ax.set_title(f"mtr per-hop -> {target}   (worst hop-loss {worst:.0f}%)",
                             loc="left", fontsize=10, fontweight="bold")
                ax.set_ylabel("avg ms/hop"); ax.set_ylim(bottom=0)
            panels.append(draw_mtr)
    if "wifi" in selected and data["wifi"]:
        def draw_wifi(ax, w=data["wifi"]):
            ax.plot(_dt(w["t"]), w["rssi"], color="#2ca02c", lw=0.9, label="RSSI dBm")
            ax.plot(_dt(w["t"]), w["noise"], color="#888888", lw=0.9, label="noise dBm")
            tx = next((v for v in reversed(w["tx"]) if v is not None), None)
            roams = w.get("roams", 0); bssids = w.get("bssids_seen", 0)
            extra = []
            if tx: extra.append(f"tx {tx:.0f} Mbit/s")
            if bssids > 1: extra.append(f"{roams} roams across {bssids} BSSIDs")
            ax.set_title(f"WiFi signal (dBm, higher=better)   {' . '.join(extra)}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("dBm")
            if any(r is not None for r in w.get("retry_rate", [])):
                ax2 = ax.twinx()
                ax2.plot(_dt(w["t"]), w["retry_rate"], color="#d62728", lw=0.7, alpha=0.6, label="retry/s")
                ax2.set_ylabel("retry/s", color="#d62728"); ax2.set_ylim(bottom=0)
                ax2.tick_params(axis="y", labelcolor="#d62728")
        panels.append(draw_wifi)
    if "iperf" in selected and data["iperf"]:
        def draw_iperf(ax, d=data["iperf"]):
            ax.plot(_dt(d["t"]), d["down"], lw=1.0, marker="o", ms=3, label="down")
            ax.plot(_dt(d["t"]), d["up"], lw=1.0, marker="o", ms=3, label="up")
            ax.set_title("iperf3 throughput (Mbit/s) - active test", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(draw_iperf)
    if "host" in selected and data["host"]:
        def draw_host(ax, d=data["host"]):
            t = _dt(d["t"])
            ax.plot(t, d["cpu"], color="#ff7f0e", lw=0.9, label="cpu %")
            ax.plot(t, d["mem"], color="#1f77b4", lw=0.9, label="mem %")
            if any(v is not None and v > 0 for v in d.get("swap", [])):
                ax.plot(t, d["swap"], color="#9467bd", lw=0.9, label="swap %")
            temp = next((v for v in reversed(d["temp"]) if v is not None), None)
            ax.set_title(f"Host cpu/mem/swap (%)   {f'temp {temp:.0f}C' if temp is not None else ''}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("%"); ax.set_ylim(bottom=0, top=100)
        panels.append(draw_host)
    if "disk" in selected and data["disk"]:
        def draw_disk(ax, disk=data["disk"]):
            for mount, d in sorted(disk.items()):
                ax.plot(_dt(d["t"]), d["used"], lw=0.9, label=mount)
                if any(v is not None and v > 0 for v in d.get("inode", [])):
                    ax.plot(_dt(d["t"]), d["inode"], lw=0.6, ls=":", label=f"{mount} inode%")
            ax.set_title("Disk used (%) per mount", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("%"); ax.set_ylim(bottom=0, top=100)
        panels.append(draw_disk)
    if "thermal" in selected and data["thermal"]:
        def draw_thermal(ax, zones=data["thermal"]):
            for zone, d in sorted(zones.items()):
                ax.plot(_dt(d["t"]), d["temp"], lw=0.9, label=zone)
            ax.set_title("Thermal zones (degC) - per sensor", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("degC")
        panels.append(draw_thermal)
    if "power" in selected and data["power"]:
        def draw_power(ax, rails=data["power"]):
            total_now = 0.0; rail_count = 0
            for rail, d in sorted(rails.items()):
                ax.plot(_dt(d["t"]), d["watts"], lw=0.9, label=rail)
                last = next((v for v in reversed(d["watts"]) if v is not None), None)
                if last is not None:
                    total_now += last; rail_count += 1
            extra = f"total {total_now:.2f} W across {rail_count} rails" if rail_count else ""
            ax.set_title(f"Power draw per rail (W)   {extra}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Watts"); ax.set_ylim(bottom=0)
        panels.append(draw_power)
    if "tcp" in selected and data["tcp"]:
        def draw_tcp(ax, d=data["tcp"]):
            t = _dt(d["t"])
            ax.plot(t, d["retrans"], color="#d62728", lw=0.9, label="retrans/s")
            ax.plot(t, d["out_rsts"], color="#ff7f0e", lw=0.7, label="out RSTs/s")
            ax.plot(t, d["udp_err"], color="#8c564b", lw=0.7, label="UDP err/s")
            ax.set_title("TCP/UDP error rates (events/s)", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("events/s"); ax.set_ylim(bottom=0)
            if any(v is not None for v in d["conntrack_pct"]):
                ax2 = ax.twinx()
                ax2.plot(t, d["conntrack_pct"], color="#17becf", lw=0.7, alpha=0.7, label="conntrack %")
                ax2.set_ylabel("conntrack %", color="#17becf"); ax2.set_ylim(0, 100)
                ax2.tick_params(axis="y", labelcolor="#17becf")
        panels.append(draw_tcp)
    if "psi" in selected and data["psi"]:
        def draw_psi(ax, d=data["psi"]):
            t = _dt(d["t"])
            ax.plot(t, d["cpu"], color="#ff7f0e", lw=0.9, label="cpu")
            ax.plot(t, d["mem"], color="#1f77b4", lw=0.9, label="mem")
            ax.plot(t, d["io"], color="#2ca02c", lw=0.9, label="io")
            ax.set_title("PSI - % time blocked on resource (avg10)",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("% blocked"); ax.set_ylim(bottom=0)
        panels.append(draw_psi)
    if "freq" in selected and data["freq"]:
        def draw_freq(ax, d=data["freq"]):
            t = _dt(d["t"])
            ax.plot(t, d["mhz"], color="#9467bd", lw=0.9, label="CPU MHz")
            ax.set_title("CPU frequency (MHz) - throttle-aware",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("MHz"); ax.set_ylim(bottom=0)
            if any(v is not None and v > 0 for v in d["throttle"]):
                ax2 = ax.twinx()
                ax2.plot(t, d["throttle"], color="#d62728", lw=0.7, label="throttle/s")
                ax2.set_ylabel("throttle/s", color="#d62728"); ax2.set_ylim(bottom=0)
                ax2.tick_params(axis="y", labelcolor="#d62728")
            bits = _pi_bits_summary(d.get("pi_bits", []))
            if bits:
                ax.text(0.99, 0.02, "Pi: " + ", ".join(bits), transform=ax.transAxes,
                        fontsize=8, color="#d62728", ha="right", va="bottom",
                        bbox=dict(facecolor="white", alpha=0.7, edgecolor="#d62728", lw=0.5))
        panels.append(draw_freq)
    return panels


def _grid_dims(n: int, cols_opt: int) -> tuple[int, int]:
    """Decide rows x cols. cols_opt=0 = auto: 1 col if n<=2, else 2 cols."""
    if n <= 0:
        return (0, 0)
    cols = cols_opt if cols_opt > 0 else (1 if n <= 2 else 2)
    cols = min(cols, n)
    rows = math.ceil(n / cols)
    return rows, cols


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
        "ping":    query.load_ping_smoke(conn, since, until, targets, node) if "ping" in sel else {},
        "net":     query.load_net(conn, since, until, node) if "net" in sel else {},
        "http":    query.load_http(conn, since, until, node) if "http" in sel else {},
        "mtr":     query.load_mtr(conn, since, until, node) if "mtr" in sel else {},
        "wifi":    query.load_wifi(conn, since, until, node) if "wifi" in sel else {},
        "iperf":   query.load_iperf(conn, since, until, node) if "iperf" in sel else {},
        "host":    query.load_host(conn, since, until, node) if "host" in sel else {},
        "disk":    query.load_disk(conn, since, until, node) if "disk" in sel else {},
        "thermal": query.load_thermal(conn, since, until, node) if "thermal" in sel else {},
        "power":   query.load_power(conn, since, until, node) if "power" in sel else {},
        "tcp":     query.load_tcp(conn, since, until, node) if "tcp" in sel else {},
        "psi":     query.load_psi(conn, since, until, node) if "psi" in sel else {},
        "freq":    query.load_freq(conn, since, until, node) if "freq" in sel else {},
    }
    conn.close()
    panels = _build(sel, data)
    if not panels:
        print("No data in selected time window.", file=sys.stderr)
        return 2

    rows, cols = _grid_dims(len(panels), getattr(opts, "cols", 0))
    span_h = (until - since) / 3600
    # Per-cell width scales with span (so dots stay distinguishable) but each cell now
    # owns only 1/cols of the total figure width, so multiply column count back in.
    cell_w = opts.width if opts.width > 0 else min(40.0, max(8.0, span_h * 2))
    fig_w = cell_w * cols
    fig_h = 3.0 * rows
    fig, axes = plt.subplots(rows, cols, figsize=(fig_w, fig_h),
                             sharex="col", squeeze=False)
    flat = [ax for row in axes for ax in row]
    for ax, draw in zip(flat, panels):
        draw(ax)
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left", fontsize=7, ncol=5, framealpha=0.85)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    # Hide unused cells when len(panels) doesn't fill the grid evenly
    for ax in flat[len(panels):]:
        ax.set_visible(False)
    fig.autofmt_xdate()
    tag = f" [{node}]" if node else ""
    fig.suptitle(f"smokemon{tag} - {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} -> "
                 f"{datetime.fromtimestamp(until):%H:%M}  ({span_h:.1f}h)  {rows}x{cols} grid",
                 fontsize=12, y=0.997)
    fig.tight_layout(rect=(0, 0, 1, 0.99))
    os.makedirs(os.path.dirname(opts.out), exist_ok=True)
    fig.savefig(opts.out, dpi=opts.dpi)
    print(f"Saved graph: {opts.out}  ({rows} rows x {cols} cols, {len(panels)} panels)")
    if not opts.no_open:
        __import__("subprocess").run(["/usr/bin/open", opts.out], check=False)
    return 0
