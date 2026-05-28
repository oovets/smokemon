"""Text TUI renderer (plotext, braille). Panels arranged on a configurable grid
(default 2 cols when terminal is wide enough, 1 col otherwise). Same panel set as
the PNG renderer: ping/net/http/mtr/wifi/iperf/host/disk/thermal/power/tcp/psi/freq."""

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
    fmt = "%H:%M" if span <= 86400 else "%m-%d %H:%M"
    t = [since + span * i / 4 for i in range(5)]  # fewer ticks per panel in grid mode
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


def _pi_bits_label(bits_list):
    return ", ".join(query.pi_bits_seen(bits_list))


def _temp_tag(temp):
    """'temp 55C (25C to throttle)' / 'temp 82C (THROTTLING)' / '' (QW4 death clock)."""
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


def _build(selected, data):  # noqa: C901
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
                cur = query.last_value(d["med"])
                cur_str = f"{cur:.1f} ms" if cur is not None else "-- ms"
                _title(f"{config.TARGET_LABELS.get(name, name)} ({name})   {cur_str} . loss {avg:.1f}%")
                _ylabel("RTT ms")
                _int_yticks(d["min"], d["med"], d["max"])
            panels.append(draw)
    if "net" in selected and data["net"]:
        def draw_net(net=data["net"]):
            for iface, s in sorted(net.items()):
                plt.plot(s["t"], s["in"], label=_L(f"{iface} down"), color="cyan", marker="braille")
                plt.plot(s["t"], s["out"], label=_L(f"{iface} up"), color="orange", marker="braille")
            _title("Bandwidth (Mbit/s)")
            _ylabel("Mbit/s")
            _int_yticks(*[s["in"] for s in net.values()], *[s["out"] for s in net.values()])
        panels.append(draw_net)
    if "http" in selected and data["http"]:
        def draw_http(http=data["http"]):
            for i, (url, d) in enumerate(sorted(http.items())):
                plt.plot(d["t"], d["ttfb"], label=_L(query.host_label(url)),
                         color=config.HTTP_COLORS[i % len(config.HTTP_COLORS)], marker="braille")
            blame = query.http_blame(http)
            tag = f"   slow: {query.HTTP_LAYER_LABELS[blame[0]]} {blame[1]:.0f} ms" if blame else ""
            _title(f"HTTP TTFB (ms){tag}")
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
                _title(f"mtr -> {target}   worst hop-loss {worst:.0f}%")
                _ylabel("ms")
                _int_yticks(*[h["avg"] for h in hops.values()])
            panels.append(draw_mtr)
    if "wifi" in selected and data["wifi"]:
        def draw_wifi(w=data["wifi"]):
            plt.plot(w["t"], w["rssi"], label=_L("RSSI dBm"), color="green+", marker="braille")
            plt.plot(w["t"], w["noise"], label=_L("noise dBm"), color=240, marker="braille")
            tx = query.last_value(w["tx"])
            snr = (w["rssi"][-1] - w["noise"][-1]) if w["rssi"] and w["noise"] \
                  and w["rssi"][-1] is not None and w["noise"][-1] is not None else None
            extras = []
            if snr is not None: extras.append(f"SNR {snr:.0f} dB")
            if tx: extras.append(f"tx {tx:.0f} Mbit/s")
            roams = w.get("roams", 0); bssids = w.get("bssids_seen", 0)
            if bssids > 1: extras.append(f"{roams} roams/{bssids} APs")
            _title(f"WiFi   {' . '.join(extras)}")
            _ylabel("dBm")
            _int_yticks(w["rssi"], w["noise"])
        panels.append(draw_wifi)
    if "iperf" in selected and data["iperf"]:
        def draw_iperf(d=data["iperf"], ping=data.get("ping", {})):
            plt.plot(d["t"], d["down"], label=_L("down"), color="cyan", marker="braille")
            plt.plot(d["t"], d["up"], label=_L("up"), color="orange", marker="braille")
            bb = query.bufferbloat(d, ping)
            tag = f"   bufferbloat {bb[0]} (+{bb[1]:.0f} ms loaded)" if bb else ""
            _title(f"iperf3 (Mbit/s){tag}")
            _ylabel("Mbit/s")
            _int_yticks(d["up"], d["down"])
        panels.append(draw_iperf)
    if "host" in selected and data["host"]:
        def draw_host(d=data["host"]):
            plt.plot(d["t"], d["cpu"], label=_L("cpu%"), color="orange+", marker="braille")
            plt.plot(d["t"], d["mem"], label=_L("mem%"), color="cyan", marker="braille")
            if any(v is not None and v > 0 for v in d.get("swap", [])):
                plt.plot(d["t"], d["swap"], label=_L("swap%"), color="magenta+", marker="braille")
            temp = query.last_value(d["temp"])
            _title(f"host cpu/mem/swap (%)   {_temp_tag(temp)}")
            _ylabel("%")
            _int_yticks(d["cpu"], d["mem"], d.get("swap", []))
        panels.append(draw_host)
    if "disk" in selected and data["disk"]:
        def draw_disk(disk=data["disk"], health=data.get("disk_health", {})):
            for mount, d in sorted(disk.items()):
                plt.plot(d["t"], d["used"], label=_L(mount), marker="braille")
            _title(f"disk used (%){_disk_tag(disk, health)}")
            _ylabel("%")
            _int_yticks(*[d["used"] for d in disk.values()])
        panels.append(draw_disk)
    if "thermal" in selected and data["thermal"]:
        def draw_thermal(zones=data["thermal"]):
            for zone, d in sorted(zones.items()):
                plt.plot(d["t"], d["temp"], label=_L(zone), marker="braille")
            _title("thermal zones (degC)")
            _ylabel("degC")
            _int_yticks(*[d["temp"] for d in zones.values()])
        panels.append(draw_thermal)
    if "power" in selected and data["power"]:
        def draw_power(rails=data["power"]):
            total = 0.0; n = 0
            for rail, d in sorted(rails.items()):
                plt.plot(d["t"], d["watts"], label=_L(rail), marker="braille")
                last = query.last_value(d["watts"])
                if last is not None:
                    total += last; n += 1
            tag = f"   total {total:.2f} W" if n else ""
            _title(f"power per rail (W){tag}")
            _ylabel("W")
            _int_yticks(*[d["watts"] for d in rails.values()])
        panels.append(draw_power)
    if "tcp" in selected and data["tcp"]:
        def draw_tcp(d=data["tcp"]):
            plt.plot(d["t"], d["retrans"], label=_L("retrans/s"), color="red", marker="braille")
            plt.plot(d["t"], d["out_rsts"], label=_L("rsts/s"), color="orange", marker="braille")
            plt.plot(d["t"], d["udp_err"], label=_L("udperr/s"), color=240, marker="braille")
            ct = query.last_value(d["conntrack_pct"])
            tag = f"   conntrack {ct:.1f}%" if ct is not None else ""
            _title(f"tcp/udp errors (events/s){tag}")
            _ylabel("events/s")
            _int_yticks(d["retrans"], d["out_rsts"], d["udp_err"])
        panels.append(draw_tcp)
    if "psi" in selected and data["psi"]:
        def draw_psi(d=data["psi"]):
            plt.plot(d["t"], d["cpu"], label=_L("cpu"), color="orange+", marker="braille")
            plt.plot(d["t"], d["mem"], label=_L("mem"), color="cyan", marker="braille")
            plt.plot(d["t"], d["io"], label=_L("io"), color="green+", marker="braille")
            _title("PSI - % time blocked (avg10)")
            _ylabel("% blocked")
            _int_yticks(d["cpu"], d["mem"], d["io"])
        panels.append(draw_psi)
    if "freq" in selected and data["freq"]:
        def draw_freq(d=data["freq"]):
            plt.plot(d["t"], d["mhz"], label=_L("CPU MHz"), color="magenta+", marker="braille")
            if any(v is not None and v > 0 for v in d["throttle"]):
                plt.plot(d["t"], d["throttle"], label=_L("throttle/s"), color="red", marker="braille")
            bits = _pi_bits_label(d.get("pi_bits", []))
            _title(f"CPU MHz   {'Pi: ' + bits if bits else ''}")
            _ylabel("MHz")
            _int_yticks(d["mhz"])
        panels.append(draw_freq)
    return panels


def _grid_dims(n: int, cols_opt: int, term_cols: int) -> tuple[int, int]:
    """Auto-grid: 2 cols if terminal >= 140 chars and we have >=3 panels, else 1 col.
    Explicit --cols N forces the count (still clamped to <= n)."""
    if n <= 0:
        return (0, 0)
    if cols_opt > 0:
        cols = min(cols_opt, n)
    else:
        cols = 2 if (term_cols >= 140 and n >= 3) else 1
    rows = math.ceil(n / cols)
    return rows, cols


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
    data = query.load_all(conn, since, until, targets, node, sel, ping_loader=query.load_ping_agg)
    conn.close()
    panels = _build(sel, data)
    if not panels:
        print("No data in selected time window.", file=sys.stderr)
        return 2
    ticks, labels = _ticks(since, until)
    cols_term, lines = shutil.get_terminal_size(fallback=(120, 40))
    rows, cols = _grid_dims(len(panels), getattr(opts, "cols", 0), cols_term)

    plt.clf()
    plt.theme("pro")
    plotsize_h = max(10, lines - opts.reserve) if opts.reserve > 0 else lines
    plt.plotsize(cols_term, plotsize_h)
    plt.subplots(rows, cols)
    for idx, draw in enumerate(panels):
        r = idx // cols + 1
        c = idx % cols + 1
        plt.subplot(r, c)
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
