"""PNG renderer (matplotlib). Panels arranged on a configurable grid (default 2 cols);
each panel keeps a 'logical width' proportional to the time span so individual samples
stay distinguishable. Set --cols 1 for the classic single-column stack."""

import json
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

# Dark theme (the hub dashboard requests it so the embedded graphs match its palette). Set
# per-render in render_png; the draw() closures read it at call time, like the tui's KIOSK.
DARK = False
# Font sizes applied to every axes (incl. the twinx right-hand axes some panels add), so
# left and right tick labels + axis labels match. Applied in both themes.
_BASE_RC = {"xtick.labelsize": 7, "ytick.labelsize": 7, "axes.labelsize": 7}
_DARK_RC = {
    # all three the same so the plot area is not a lighter box on a darker figure; matches the
    # dashboard modal card (--card #11151c) so the embedded PNG blends in seamlessly.
    "figure.facecolor": "#11151c", "axes.facecolor": "#11151c", "savefig.facecolor": "#11151c",
    "text.color": "#c9d1d9", "axes.labelcolor": "#c9d1d9", "axes.titlecolor": "#c9d1d9",
    "xtick.color": "#9aa4b2", "ytick.color": "#9aa4b2",
    # no visible frame: spines blend into the facecolor so each panel is borderless
    "axes.edgecolor": "#11151c", "grid.color": "#3a4150",
    "legend.facecolor": "#11151c", "legend.edgecolor": "#2a2f3a",
}


def _dt(ts_list):
    return [datetime.fromtimestamp(t) for t in ts_list]


def _temp_tag(temp):
    """QW4 death clock: headroom from the current temp to the throttle threshold."""
    if temp is None:
        return ""
    head = config.THROTTLE_TEMP_C - temp
    return f"temp {temp:.0f}C ({head:.0f}C to throttle)" if head > 0 else f"temp {temp:.0f}C (THROTTLING)"


def _disk_tag(disk, health):
    """QW4 death clocks: soonest mount-full + SD-wear countdown, as a title suffix."""
    bits = []
    full = query.disk_full_eta(disk)
    if full:
        bits.append(f"{full[0]} full {query.human_eta(full[1])}")
    wear = query.wear_eta(health)
    if wear:
        bits.append(f"sd wear {query.human_eta(wear[1])}")
    return "   " + " . ".join(bits) if bits else ""


def _build(selected, data):  # noqa: C901 -- straight-line dispatch, intentionally flat
    panels = []
    if "ping" in selected:
        for target, d in sorted(data["ping"].items()):
            def draw(ax, d=d, target=target):
                t = _dt(d["t"])
                ax.fill_between(t, d["p0"], d["p100"], color="#3b6ea5",
                                alpha=0.25 if DARK else 0.18, label="min-max")
                ax.fill_between(t, d["p25"], d["p75"], color="#3b6ea5",
                                alpha=0.45 if DARK else 0.35, label="p25-p75")
                # the light-theme median is near-black navy; brighten it so it shows on dark.
                ax.plot(t, d["p50"], color="#8ab4f8" if DARK else "#15324f", lw=1.0, label="median")
                loss = np.array(d["loss"]); m = loss > 0
                if m.any():
                    ax.scatter(np.array(t)[m], np.array(d["p50"])[m], c=loss[m], cmap="autumn_r",
                               vmin=0, vmax=100, s=18, zorder=5, edgecolors="k", linewidths=0.3, label="loss")
                avg = float(np.nanmean(loss)) if len(loss) else 0.0
                ax.set_title(f"{config.TARGET_LABELS.get(target, target)} ({target})   avg loss {avg:.1f}%",
                             loc="left", fontsize=10, fontweight="bold")
                ax.set_ylabel("RTT (ms)"); ax.set_ylim(bottom=0)
            panels.append(("ping", draw))
    if "net" in selected and data["net"]:
        def draw_net(ax, net=data["net"]):
            for iface, s in sorted(net.items()):
                ax.plot(_dt(s["t"]), s["in"], lw=0.9, label=f"{iface} down")
                ax.plot(_dt(s["t"]), s["out"], lw=0.9, ls="--", label=f"{iface} up")
            ax.set_title("Bandwidth per interface (Mbit/s)", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(("net", draw_net))
    if "http" in selected and data["http"]:
        def draw_http(ax, http=data["http"]):
            for url, d in sorted(http.items()):
                ax.plot(_dt(d["t"]), d["ttfb"], lw=0.9, label=query.host_label(url))
            blame = query.http_blame(http)
            tag = f"   slowest layer: {query.HTTP_LAYER_LABELS[blame[0]]} {blame[1]:.0f} ms" if blame else ""
            ax.set_title(f"HTTP TTFB (ms) - time to first byte{tag}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("ms"); ax.set_ylim(bottom=0)
        panels.append(("http", draw_http))
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
            panels.append(("mtr", draw_mtr))
    if "wifi" in selected and data["wifi"]:
        def draw_wifi(ax, w=data["wifi"]):
            ax.plot(_dt(w["t"]), w["rssi"], color="#2ca02c", lw=0.9, label="RSSI dBm")
            ax.plot(_dt(w["t"]), w["noise"], color="#888888", lw=0.9, label="noise dBm")
            tx = query.last_value(w["tx"])
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
        panels.append(("wifi", draw_wifi))
    if "iperf" in selected and data["iperf"]:
        def draw_iperf(ax, d=data["iperf"], ping=data.get("ping", {})):
            ax.plot(_dt(d["t"]), d["down"], lw=1.0, marker="o", ms=3, label="down")
            ax.plot(_dt(d["t"]), d["up"], lw=1.0, marker="o", ms=3, label="up")
            bb = query.bufferbloat(d, ping)
            tag = f"   bufferbloat {bb[0]} (+{bb[1]:.0f} ms under load)" if bb else ""
            ax.set_title(f"iperf3 throughput (Mbit/s) - active test{tag}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Mbit/s"); ax.set_ylim(bottom=0)
        panels.append(("iperf", draw_iperf))
    if "host" in selected and data["host"]:
        def draw_host(ax, d=data["host"]):
            t = _dt(d["t"])
            ax.plot(t, d["cpu"], color="#ff7f0e", lw=0.9, label="cpu %")
            ax.plot(t, d["mem"], color="#1f77b4", lw=0.9, label="mem %")
            if any(v is not None and v > 0 for v in d.get("swap", [])):
                ax.plot(t, d["swap"], color="#9467bd", lw=0.9, label="swap %")
            temp = query.last_value(d["temp"])
            ax.set_title(f"Host cpu/mem/swap (%)   {_temp_tag(temp)}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("%"); ax.set_ylim(bottom=0, top=100)
        panels.append(("host", draw_host))
    if "gpu" in selected and data.get("gpu"):
        def draw_gpu(ax, gpus=data["gpu"]):
            t_for_freq = None
            freq_series = []
            for gpu, d in sorted(gpus.items()):
                t = _dt(d["t"])
                ax.plot(t, d["util"], lw=0.9, label=f"{gpu} util %")
                if any(v is not None for v in d.get("freq", [])):
                    t_for_freq = t
                    freq_series = d["freq"]
            cur = max((query.last_value(d["util"]) or 0.0) for d in gpus.values())
            ax.set_title(f"Jetson GPU util/frequency   util {cur:.0f}%",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("util %"); ax.set_ylim(bottom=0, top=100)
            if t_for_freq and freq_series:
                ax2 = ax.twinx()
                ax2.plot(t_for_freq, freq_series, color="#9467bd", lw=0.7, ls="--", label="MHz")
                ax2.set_ylabel("MHz", color="#9467bd"); ax2.set_ylim(bottom=0)
                ax2.tick_params(axis="y", labelcolor="#9467bd")
        panels.append(("gpu", draw_gpu))
    if "redis" in selected and data.get("redis"):
        def draw_redis(ax, r=data["redis"]):
            streams = r.get("streams", {})
            for name, d in sorted(streams.items()):
                label = d.get("stream", name).rsplit(":", 1)[-1]
                ax.plot(_dt(d["t"]), d["xlen"], lw=0.9, label=label)
                if any(v is not None and v > 0 for v in d.get("pending", [])):
                    ax.plot(_dt(d["t"]), d["pending"], lw=0.8, ls=":", label=f"{label} pending")
            max_x = max((query.last_value(d["xlen"]) or 0 for d in streams.values()), default=0)
            mem = max((query.last_value(d["mem"]) or 0 for d in r.get("server", {}).values()), default=0)
            tag = f"max xlen {max_x}" + (f" . redis {mem:.0f} MB" if mem else "")
            ax.set_title(f"Redis streams   {tag}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("entries"); ax.set_ylim(bottom=0)
        panels.append(("redis", draw_redis))
    if "disk" in selected and data["disk"]:
        def draw_disk(ax, disk=data["disk"], health=data.get("disk_health", {})):
            # no per-mount legend: a busy box (many loop/snap mounts) drowns the panel; the
            # mounts + death-clock live in the title/tooltip instead.
            for _mount, d in sorted(disk.items()):
                ax.plot(_dt(d["t"]), d["used"], lw=0.9)
                if any(v is not None and v > 0 for v in d.get("inode", [])):
                    ax.plot(_dt(d["t"]), d["inode"], lw=0.6, ls=":")
            ax.set_title(f"Disk used (%) per mount{_disk_tag(disk, health)}",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("%"); ax.set_ylim(bottom=0, top=100)
        panels.append(("disk", draw_disk))
    if "thermal" in selected and data["thermal"]:
        def draw_thermal(ax, zones=data["thermal"]):
            for zone, d in sorted(zones.items()):
                ax.plot(_dt(d["t"]), d["temp"], lw=0.9, label=zone)
            ax.set_title("Thermal zones (degC) - per sensor", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("degC")
        panels.append(("thermal", draw_thermal))
    if "power" in selected and data["power"]:
        def draw_power(ax, rails=data["power"]):
            total_now = 0.0; rail_count = 0
            for rail, d in sorted(rails.items()):
                ax.plot(_dt(d["t"]), d["watts"], lw=0.9, label=rail)
                last = query.last_value(d["watts"])
                if last is not None:
                    total_now += last; rail_count += 1
            extra = f"total {total_now:.2f} W across {rail_count} rails" if rail_count else ""
            ax.set_title(f"Power draw per rail (W)   {extra}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("Watts"); ax.set_ylim(bottom=0)
        panels.append(("power", draw_power))
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
        panels.append(("tcp", draw_tcp))
    if "psi" in selected and data["psi"]:
        def draw_psi(ax, d=data["psi"]):
            t = _dt(d["t"])
            ax.plot(t, d["cpu"], color="#ff7f0e", lw=0.9, label="cpu")
            ax.plot(t, d["mem"], color="#1f77b4", lw=0.9, label="mem")
            ax.plot(t, d["io"], color="#2ca02c", lw=0.9, label="io")
            ax.set_title("PSI - % time blocked on resource (avg10)",
                         loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("% blocked"); ax.set_ylim(bottom=0)
        panels.append(("psi", draw_psi))
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
            bits = query.pi_bits_seen(d.get("pi_bits", []))
            if bits:
                ax.text(0.99, 0.02, "Pi: " + ", ".join(bits), transform=ax.transAxes,
                        fontsize=8, color="#d62728", ha="right", va="bottom",
                        bbox=dict(facecolor="white", alpha=0.7, edgecolor="#d62728", lw=0.5))
        panels.append(("freq", draw_freq))
    if "self" in selected and data.get("self"):
        def draw_self(ax, d=data["self"]):
            t = _dt(d["t"])
            ax.plot(t, d["rss"], color="#9467bd", lw=0.9, label="rss MB")
            rss = query.last_value(d["rss"])
            tag = f"   rss {rss:.0f} MB now" if rss is not None else ""
            ax.set_title(f"smokemon self-footprint{tag}", loc="left", fontsize=10, fontweight="bold")
            ax.set_ylabel("MB"); ax.set_ylim(bottom=0)
            if any(v is not None and v > 0 for v in d["cpu"]):
                ax2 = ax.twinx()
                ax2.plot(t, d["cpu"], color="#ff7f0e", lw=0.7, label="cpu %")
                ax2.set_ylabel("cpu %", color="#ff7f0e"); ax2.set_ylim(bottom=0)
                ax2.tick_params(axis="y", labelcolor="#ff7f0e")
        panels.append(("self", draw_self))
    return panels


def _grid_dims(n: int, cols_opt: int) -> tuple[int, int]:
    """Decide rows x cols. cols_opt=0 = auto: 1 col if n<=2, else 2 cols."""
    if n <= 0:
        return (0, 0)
    cols = cols_opt if cols_opt > 0 else (1 if n <= 2 else 2)
    cols = min(cols, n)
    rows = math.ceil(n / cols)
    return rows, cols


def render_png(opts) -> tuple[bytes | None, list]:
    """Build the panel grid; return (PNG bytes, panel-meta). PNG is None if the window holds
    no data. Shared by `smoke png` (writes a file) and the hub's GET /api/png. theme='dark'
    renders on the dashboard's dark palette. With opts.meta the per-panel titles are pulled
    OFF the image and returned as meta (each panel's position in 0..1 fractions + its title
    text) so the dashboard can show them as hover tooltips instead."""
    global DARK
    import io
    DARK = getattr(opts, "theme", "light") == "dark"
    want_meta = getattr(opts, "meta", False)
    since, until = query.window(opts.hours, opts.minutes, opts.since, opts.until)
    targets = [t.strip() for t in opts.targets.split(",")] if opts.targets else None
    sel = ALL_PANELS if opts.panels == "all" else [s.strip() for s in opts.panels.split(",")]
    node = opts.node
    conn = query.open_ro(opts.db)
    data = query.load_all(conn, since, until, targets, node, sel, ping_loader=query.load_ping_smoke)
    conn.close()
    panels = _build(sel, data)
    if not panels:
        return None, []

    rows, cols = _grid_dims(len(panels), getattr(opts, "cols", 0))
    span_h = (until - since) / 3600
    # Per-cell width scales with span (so dots stay distinguishable) but each cell now
    # owns only 1/cols of the total figure width, so multiply column count back in.
    cell_w = opts.width if opts.width > 0 else min(40.0, max(8.0, span_h * 2))
    # Row height tracks column width to keep each panel ~2.6:1, so a narrow 3-col grid gets
    # proportionally shorter rows. Clamped so wide (1-2 col / span-scaled Preview) panels keep
    # the familiar 3in height and don't balloon.
    row_h = max(2.0, min(3.0, cell_w / 2.6))
    with matplotlib.rc_context({**_BASE_RC, **(_DARK_RC if DARK else {})}):
        fig, axes = plt.subplots(rows, cols, figsize=(cell_w * cols, row_h * rows),
                                 sharex="col", squeeze=False)
        flat = [ax for row in axes for ax in row]
        drawn = []  # (ax, title) for meta after tight_layout settles positions
        for ax, (key, draw) in zip(flat, panels):
            draw(ax)
            title = ax.get_title(loc="left")
            if want_meta:
                ax.set_title("", loc="left")  # off the image -> into a hover tooltip instead
            # no gridlines in the dark/web theme (clean look). Pass alpha ONLY when enabling:
            # matplotlib treats grid(False, <line-props>) as "enable anyway", so the alpha kwarg
            # would silently re-add the grid we are trying to drop.
            if DARK:
                ax.grid(False)
            else:
                ax.grid(True, alpha=0.25)
            handles, labels = ax.get_legend_handles_labels()
            if labels:
                # Compact legend, loc="best" so it dodges the data. Keeps every series (mtr/
                # http/disk can have many) but scales down: high-cardinality panels get a
                # smaller font + more columns so it stays a flat strip, not a panel-swamping box.
                many = len(labels) > 8
                ax.legend(handles, labels, loc="best",
                          fontsize=5 if many else 6,
                          ncol=min(5 if many else 3, len(labels)),
                          framealpha=0.5, labelspacing=0.2, columnspacing=0.8,
                          handlelength=1.2, handletextpad=0.35, borderpad=0.25)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            drawn.append((ax, title, key))
        # Hide unused cells when len(panels) doesn't fill the grid evenly
        for ax in flat[len(panels):]:
            ax.set_visible(False)
        fig.autofmt_xdate()
        have_title = not getattr(opts, "no_title", False)
        if have_title:
            tag = f" [{node}]" if node else ""
            fig.suptitle(f"smokemon{tag} - {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} -> "
                         f"{datetime.fromtimestamp(until):%H:%M}  ({span_h:.1f}h)  {rows}x{cols} grid",
                         fontsize=12, y=0.997)
        fig.tight_layout(pad=0.3, w_pad=0.4, h_pad=0.5,
                         rect=(0, 0, 1, 0.99 if have_title else 1.0))
        meta = []
        if want_meta:
            # Final axes positions as figure fractions (y flipped to top-origin for the web
            # overlay). Percent-based, so the dashboard's overlay boxes scale with the image.
            for ax, title, key in drawn:
                p = ax.get_position()
                meta.append({"x": round(p.x0, 4), "y": round(1 - (p.y0 + p.height), 4),
                             "w": round(p.width, 4), "h": round(p.height, 4), "t": title, "k": key})
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=opts.dpi)
        plt.close(fig)   # free the figure (matters in a long render loop / subprocess)
    return buf.getvalue(), meta


def run(opts) -> int:
    if not os.path.exists(opts.db):
        print(f"No database found: {opts.db}", file=sys.stderr)
        return 1
    png, meta = render_png(opts)
    if png is None:
        print("No data in selected time window.", file=sys.stderr)
        return 2
    if opts.out == "-":                       # stream raw PNG to stdout (used by GET /api/png)
        sys.stdout.buffer.write(png)
        if getattr(opts, "meta", False):      # panel tooltips: emit meta on stderr for the hub
            sys.stderr.write("SMOKEMON_META " + json.dumps(meta) + "\n")
        return 0
    os.makedirs(os.path.dirname(opts.out), exist_ok=True)
    with open(opts.out, "wb") as f:
        f.write(png)
    print(f"Saved graph: {opts.out}")
    if not opts.no_open:
        __import__("subprocess").run(["/usr/bin/open", opts.out], check=False)
    return 0
