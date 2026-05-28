"""Text TUI renderer (plotext, braille). Panels: ping/net/http/mtr/wifi/iperf/host/disk."""

import math
import os
import shutil
import sys
from datetime import datetime

import plotext as plt

from .. import config, query

ALL_PANELS = config.PANELS
KIOSK = False


def _L(s):
    return None if KIOSK else s


def _title(s):
    if not KIOSK:
        plt.title(s)


def _ylabel(s):
    if not KIOSK:
        plt.ylabel(s)


def _ticks(since, until):
    span = until - since
    fmt = "%H:%M" if span <= 86400 else "%m-%d %H:%M"  # never seconds
    t = [since + span * i / 6 for i in range(7)]
    return t, [datetime.fromtimestamp(x).strftime(fmt) for x in t]


def _int_yticks(*lists):
    if KIOSK:
        return
    vals = [v for lst in lists for v in lst if v is not None and v == v]
    if not vals:
        return
    lo, hi = min(vals), max(vals)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    step = max(1, round((hi - lo) / 5))
    ticks = list(range(math.floor(lo), math.ceil(hi) + 1, step))
    if len(ticks) >= 2:
        plt.yticks(ticks, [str(t) for t in ticks])


def _build(selected, node, data):
    panels = []
    if "ping" in selected:
        for name, d in sorted(data["ping"].items()):
            def draw(d=d, name=name):
                plt.plot(d["t"], d["max"], color=240, marker="braille")
                plt.plot(d["t"], d["min"], color=240, marker="braille")
                plt.plot(d["t"], d["med"], label=_L("median"), color="orange+", marker="braille")
                lt = [t for t, l in zip(d["t"], d["loss"]) if l > 0]
                lm = [m for m, l in zip(d["med"], d["loss"]) if l > 0]
                if lt:
                    plt.scatter(lt, lm, color="red", marker="braille", label=_L("loss"))
                avg = sum(d["loss"]) / len(d["loss"]) if d["loss"] else 0.0
                cur = d["med"][-1] if d["med"] else float("nan")
                _title(f"{config.TARGET_LABELS.get(name, name)} ({name})   median now {cur:.1f} ms "
                       f"· spread (gray) min–max · avg loss {avg:.1f}%")
                _ylabel("RTT ms")
                _int_yticks(d["min"], d["med"], d["max"])
            panels.append(draw)
    if "net" in selected and data["net"]:
        def draw_net(net=data["net"]):
            for iface, s in sorted(net.items()):
                plt.plot(s["t"], s["in"], label=_L(f"{iface} down"), color="cyan", marker="braille")
                plt.plot(s["t"], s["out"], label=_L(f"{iface} up"), color="orange", marker="braille")
            _title("Bandwidth (Mbit/s) — passive, actual traffic")
            _ylabel("Mbit/s")
            _int_yticks(*[s["in"] for s in net.values()], *[s["out"] for s in net.values()])
        panels.append(draw_net)
    if "http" in selected and data["http"]:
        def draw_http(http=data["http"]):
            for i, (url, d) in enumerate(sorted(http.items())):
                plt.plot(d["t"], d["ttfb"], label=_L(query.host_label(url)),
                         color=config.HTTP_COLORS[i % len(config.HTTP_COLORS)], marker="braille")
            _title("HTTP TTFB (ms) — time to first byte (DNS+TCP+TLS+server)")
            _ylabel("ms")
            _int_yticks(*[d["ttfb"] for d in http.values()])
        panels.append(draw_http)
    if "mtr" in selected:
        for target, hops in sorted(data["mtr"].items()):
            def draw_mtr(hops=hops, target=target):
                worst = 0.0
                for hop_no in sorted(hops):
                    h = hops[hop_no]
                    plt.plot(h["t"], h["avg"], label=_L(f"h{hop_no} {h['host']}" if h.get("host") else f"h{hop_no}"),
                             marker="braille")
                    if h["loss"]:
                        worst = max(worst, max(h["loss"]))
                _title(f"mtr per-hop → {target}   (avg latency/hop, worst hop-loss {worst:.0f}%)")
                _ylabel("ms")
                _int_yticks(*[h["avg"] for h in hops.values()])
            panels.append(draw_mtr)
    if "wifi" in selected and data["wifi"]:
        def draw_wifi(w=data["wifi"]):
            plt.plot(w["t"], w["rssi"], label=_L("RSSI dBm"), color="green+", marker="braille")
            plt.plot(w["t"], w["noise"], label=_L("noise dBm"), color=240, marker="braille")
            tx = next((v for v in reversed(w["tx"]) if v is not None), None)
            snr = (w["rssi"][-1] - w["noise"][-1]) if w["rssi"] and w["noise"] else None
            extra = (f"SNR {snr:.0f} dB" if snr is not None else "") + (f" · tx {tx:.0f} Mbit/s" if tx else "")
            _title(f"WiFi signal (dBm, higher=better)   {extra}")
            _ylabel("dBm")
            _int_yticks(w["rssi"], w["noise"])
        panels.append(draw_wifi)
    if "iperf" in selected and data["iperf"]:
        def draw_iperf(d=data["iperf"]):
            plt.plot(d["t"], d["down"], label=_L("down"), color="cyan", marker="braille")
            plt.plot(d["t"], d["up"], label=_L("up"), color="orange", marker="braille")
            _title("iperf3 throughput (Mbit/s) — active test to peer")
            _ylabel("Mbit/s")
            _int_yticks(d["up"], d["down"])
        panels.append(draw_iperf)
    if "host" in selected and data["host"]:
        def draw_host(d=data["host"]):
            plt.plot(d["t"], d["cpu"], label=_L("cpu %"), color="orange+", marker="braille")
            plt.plot(d["t"], d["mem"], label=_L("mem %"), color="cyan", marker="braille")
            temp = next((v for v in reversed(d["temp"]) if v is not None), None)
            _title(f"Host cpu/mem (%)   {f'temp {temp:.0f}°C' if temp is not None else ''}")
            _ylabel("%")
            _int_yticks(d["cpu"], d["mem"])
        panels.append(draw_host)
    if "disk" in selected and data["disk"]:
        def draw_disk(disk=data["disk"]):
            for mount, d in sorted(disk.items()):
                plt.plot(d["t"], d["used"], label=_L(mount), marker="braille")
            _title("Disk used (%) per mount")
            _ylabel("%")
            _int_yticks(*[d["used"] for d in disk.values()])
        panels.append(draw_disk)
    return panels


def run(opts) -> int:
    global KIOSK
    KIOSK = opts.kiosk
    if not os.path.exists(opts.db):
        print(f"No database found: {opts.db}", file=sys.stderr)
        return 1
    since, until = query.window(opts.hours, opts.minutes, opts.since, opts.until)
    targets = [t.strip() for t in opts.targets.split(",")] if opts.targets else None
    sel = ALL_PANELS if opts.panels == "all" else [s.strip() for s in opts.panels.split(",")]
    node = opts.node
    conn = query.open_ro(opts.db)
    data = {
        "ping": query.load_ping_agg(conn, since, until, targets, node) if "ping" in sel else {},
        "net": query.load_net(conn, since, until, node) if "net" in sel else {},
        "http": query.load_http(conn, since, until, node) if "http" in sel else {},
        "mtr": query.load_mtr(conn, since, until, node) if "mtr" in sel else {},
        "wifi": query.load_wifi(conn, since, until, node) if "wifi" in sel else {},
        "iperf": query.load_iperf(conn, since, until, node) if "iperf" in sel else {},
        "host": query.load_host(conn, since, until, node) if "host" in sel else {},
        "disk": query.load_disk(conn, since, until, node) if "disk" in sel else {},
    }
    conn.close()
    panels = _build(sel, node, data)
    if not panels:
        print("No data in selected time window.", file=sys.stderr)
        return 2
    ticks, labels = _ticks(since, until)
    plt.clf()
    plt.theme("pro")
    if opts.reserve > 0:
        cols, lines = shutil.get_terminal_size(fallback=(120, 40))
        plt.plotsize(cols, max(10, lines - opts.reserve))
    plt.subplots(len(panels), 1)
    for idx, draw in enumerate(panels, start=1):
        plt.subplot(idx, 1)
        plt.xlim(since, until)
        if KIOSK:
            plt.frame(True)
            plt.ticks_color(240)
            plt.xfrequency(0)
            plt.yfrequency(0)
        else:
            plt.xticks(ticks, labels)
        draw()
    plt.show()
    return 0
