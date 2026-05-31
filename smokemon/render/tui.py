"""Text TUI renderer (plotext, braille). Panels arranged on a configurable grid
(default 2 cols when terminal is wide enough, 1 col otherwise). Same panel set as
the PNG renderer: ping/net/http/mtr/wifi/iperf/host/gpu/redis/docker/pipeline/disk/
thermal/power/tcp/psi/freq."""

import math
import os
import shutil
import sys
from datetime import datetime

import plotext as plt

from .. import config, query

ALL_PANELS = config.PANELS

# Distinct, legible xterm-256 palette for panels that overlay an arbitrary number of series
# (mtr hops, docker containers, redis streams, disk mounts, thermal zones, power rails,
# pipeline procs, GPUs). Cycled per series so neighbouring lines stay separable instead of
# falling back to plotext's low-contrast default cycle. plotext emits these as 38;5;N, which
# the hub dashboard's ANSI->rgb parser renders directly, and they read well on the dark theme.
SERIES_COLORS = [51, 208, 46, 201, 226, 39, 214, 141, 49, 213, 123, 220, 165, 84]


def _scolor(i: int) -> int:
    return SERIES_COLORS[i % len(SERIES_COLORS)]


KIOSK = False
# Suppress series labels (-> plotext draws no legend). The web /api/plot retries with this on
# if a normal render crashes: some panels' degenerate series (e.g. a 100%-loss target with an
# all-None median, or an all-None wifi sub-series) trip a plotext legend-build IndexError.
NOLEGEND = False
# Drop the per-panel frame/border (the hub GUI plot view wants a clean, borderless look).
NOFRAME = False


def _reset_plotext():
    """Fully reset plotext's global figure. plt.clf() only clears the *active* subplot, so
    in the live loop (one long-lived process, render per refresh) the other subplots' data
    accumulates across frames — doubled legends, panels repeated/garbled. Recreating the
    whole figure clears everything. Falls back to clf() if plotext internals change."""
    try:
        import plotext._core as _core
        _core._figure = _core._figure_class()
    except Exception:  # noqa: BLE001 - never let a plotext internal change break rendering
        plt.clf()


def _L(s):
    return None if (KIOSK or NOLEGEND) else s


def _title(s):
    # Kiosk keeps a minimal title — just the panel label (the part before the live-stats
    # suffix, which every panel separates with a 3-space gap) so you can still tell what
    # each graph shows without the noisy current-value readout.
    plt.title(s.split("   ")[0] if KIOSK else s)


def _ylabel(s):
    if not KIOSK:
        plt.ylabel(s)


def _ticks(since, until):
    span = until - since
    fmt = "%H:%M" if span <= 86400 else "%m-%d %H:%M"
    t = [since + span * i / 4 for i in range(5)]  # fewer ticks per panel in grid mode
    return t, [datetime.fromtimestamp(x).strftime(fmt) for x in t]


def _int_yticks(*lists):
    """Pad the y-axis and lay down readable integer ticks. plotext autoscales the y-axis to the
    exact data min/max, which glues curves to the top/bottom frame and turns an all-equal series
    into a degenerate (zero-height) axis. We pad ~8% (or expand a flat series), never dip a
    non-negative metric below zero, then place a handful of integer ticks across the real range."""
    if KIOSK:
        return
    vals = [v for lst in lists for v in lst if v is not None and v == v]
    if not vals:
        return
    mn, mx = min(vals), max(vals)
    if mn == mx:                       # flat/all-equal: give the line room instead of a 0-height axis
        pad = abs(mn) * 0.05 or 1.0
        lo, hi = mn - pad, mx + pad
    else:                              # padded so spiky/normal curves don't touch the frame
        pad = (mx - mn) * 0.08
        lo, hi = mn - pad, mx + pad
    if mn >= 0 and lo < 0:             # rtt/%, counts, bytes: keep the baseline at zero, not negative
        lo = 0.0
    plt.ylim(lo, hi)
    step = max(1, round((hi - lo) / 5))
    ticks = list(range(math.floor(mn), math.ceil(mx) + 1, step))
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


def _data_xspan(data):
    """Earliest/latest sample timestamp across every loaded series. The x-axis fits this actual
    span (like the PNG renderer's autoscale) instead of the full requested window - otherwise a
    24h window holding only ~1h of data squishes every graph into the rightmost sliver. Using one
    global span (not per-panel) keeps all panels sharing the same x-axis, so they stay aligned."""
    lo = hi = None

    def scan(o):
        nonlocal lo, hi
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "t" and isinstance(v, list) and v:
                    m0, m1 = min(v), max(v)
                    lo = m0 if lo is None else min(lo, m0)
                    hi = m1 if hi is None else max(hi, m1)
                else:
                    scan(v)
        elif isinstance(o, list):
            for x in o:
                if isinstance(x, (dict, list)):
                    scan(x)

    scan(data)
    return lo, hi


def _downsample(data, n):
    """Bucket-average every series down to ~n points, in place. plotext plots every sample, so a
    24h window with thousands of points smears each braille column into a vertical block (matplotlib
    draws a continuous line, which is why the PNG looks clean). Reducing to ~2x the panel's column
    width restores a readable line. Mutates the same leaf dicts the _build closures captured, so it
    can run after the grid size is known but before draw()."""
    def reduce(d):
        t = d.get("t")
        if not isinstance(t, list) or len(t) <= n:
            return
        k = len(t)
        bounds = [round(i * k / n) for i in range(n + 1)]
        keys = [key for key, v in d.items() if isinstance(v, list) and len(v) == k]
        new = {key: [] for key in keys}
        for b in range(n):
            lo, hi = bounds[b], bounds[b + 1]
            if hi <= lo:
                continue
            for key in keys:
                nums = [x for x in d[key][lo:hi] if isinstance(x, (int, float)) and not isinstance(x, bool)]
                new[key].append(sum(nums) / len(nums) if nums else None)
        for key in keys:
            d[key] = new[key]

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("t"), list):
                reduce(o)
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)


def _build(selected, data):  # noqa: C901
    panels = []
    if "ping" in selected:
        for name, d in sorted(data["ping"].items()):
            def draw(d=d, name=name):
                # plotext (unlike matplotlib) crashes on all-None series and on scatter points
                # with None y. A target at 100% loss has median/min/max all None, so guard every
                # series + drop loss markers that have no RTT to sit on.
                if any(v is not None for v in d["max"]):
                    plt.plot(d["t"], d["max"], color=240, marker="braille")
                if any(v is not None for v in d["min"]):
                    plt.plot(d["t"], d["min"], color=240, marker="braille")
                if any(v is not None for v in d["med"]):
                    plt.plot(d["t"], d["med"], label=_L("median"), color="orange+", marker="braille")
                pts = [(t, m) for t, m, l in zip(d["t"], d["med"], d["loss"]) if l > 0 and m is not None]
                if pts:
                    plt.scatter([p[0] for p in pts], [p[1] for p in pts],
                                color="red", marker="braille", label=_L("loss"))
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
                for i, hop_no in enumerate(sorted(hops)):
                    h = hops[hop_no]
                    plt.plot(h["t"], h["avg"], label=_L(f"h{hop_no} {h['host']}" if h.get("host") else f"h{hop_no}"),
                             color=_scolor(i), marker="braille")
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
    if "gpu" in selected and data.get("gpu"):
        def draw_gpu(gpus=data["gpu"]):
            for i, (gpu, d) in enumerate(sorted(gpus.items())):
                plt.plot(d["t"], d["util"], label=_L(f"{gpu} util%"),
                         color="green+" if len(gpus) == 1 else _scolor(i), marker="braille")
            cur = max((query.last_value(d["util"]) or 0.0) for d in gpus.values())
            _title(f"Jetson GPU   util {cur:.0f}%")
            _ylabel("%")
            _int_yticks(*[d["util"] for d in gpus.values()])
        panels.append(draw_gpu)
    if "redis" in selected and data.get("redis"):
        def draw_redis(r=data["redis"]):
            streams = r.get("streams", {})
            server = r.get("server", {})
            mem = max((query.last_value(d["mem"]) or 0 for d in server.values()), default=0)
            clients = max((query.last_value(d.get("clients", [])) or 0 for d in server.values()), default=0)
            memtag = (f" . mem {mem:.0f} MB" if mem else "") + (f" . {clients:.0f} cli" if clients else "")
            if streams:
                for i, (name, d) in enumerate(sorted(streams.items())):
                    label = d.get("stream", name).rsplit(":", 1)[-1]
                    plt.plot(d["t"], d["xlen"], label=_L(label), color=_scolor(i), marker="braille")
                    if any(v is not None and v > 0 for v in d.get("pending", [])):
                        plt.plot(d["t"], d["pending"], label=_L(f"{label} pending"), color="red", marker="braille")
                max_x = max((query.last_value(d["xlen"]) or 0 for d in streams.values()), default=0)
                _title(f"Redis streams   max xlen {max_x}{memtag}")
                _ylabel("entries")
                _int_yticks(*[d["xlen"] for d in streams.values()], *[d.get("pending", []) for d in streams.values()])
            else:
                # no streams configured: plot server throughput so the panel still shows load.
                for d in server.values():
                    if any(v is not None for v in d.get("ops", [])):
                        plt.plot(d["t"], d["ops"], label=_L("ops/s"), color="cyan", marker="braille")
                _title(f"Redis   ops/s{memtag}")
                _ylabel("ops/s")
                _int_yticks(*[d.get("ops", []) for d in server.values()])
        panels.append(draw_redis)
    if "docker" in selected and data.get("docker"):
        def draw_docker(dk=data["docker"]):
            have_cpu = any(any(v is not None for v in d.get("cpu", [])) for d in dk.values())
            running_now = sum(1 for d in dk.values() if query.last_value(d.get("running", [])))
            stopped = len(dk) - running_now
            if have_cpu:
                for i, (name, d) in enumerate(sorted(dk.items())):
                    if any(v is not None for v in d.get("cpu", [])):
                        plt.plot(d["t"], d["cpu"], label=_L(name), color=_scolor(i), marker="braille")
                _ylabel("cpu %")
                _int_yticks(*[d["cpu"] for d in dk.values()])
            else:
                ts, counts = query.docker_running_timeline(dk)
                plt.plot(ts, counts, label=_L("running"), color="green+", marker="braille")
                _ylabel("containers")
                _int_yticks(counts)
            tag = f"   {running_now}/{len(dk)} up" + (f" . {stopped} stopped" if stopped else "")
            _title(f"Docker{tag}")
        panels.append(draw_docker)
    if "pipeline" in selected and data.get("pipeline"):
        def draw_pipeline(p=data["pipeline"]):
            procs = p.get("procs", {})
            streams = p.get("streams", {})
            drew_cpu = False
            for i, (label, d) in enumerate(sorted(procs.items())):
                if any(v is not None for v in d.get("cpu", [])):
                    plt.plot(d["t"], d["cpu"], label=_L(f"{label} cpu%"), color=_scolor(i), marker="braille")
                    drew_cpu = True
            if not drew_cpu:
                # no process CPU yet: show stream latency so the panel isn't blank.
                for i, (url, d) in enumerate(sorted(streams.items())):
                    if any(v is not None for v in d.get("latency", [])):
                        plt.plot(d["t"], d["latency"], label=_L(f"{query.host_label(url)} ms"),
                                 color=_scolor(i), marker="braille")
                _ylabel("stream ms")
            else:
                _ylabel("cpu %")
            down = [label for label, d in procs.items() if not (query.last_value(d.get("count", [])) or 0)]
            tag = f"   {len(procs)} watch" + (f" . {len(down)} down" if down else "")
            _title(f"Pipeline{tag}")
            _int_yticks(*[d.get("cpu", []) for d in procs.values()],
                        *[d.get("latency", []) for d in streams.values()])
        panels.append(draw_pipeline)
    if "disk" in selected and data["disk"]:
        def draw_disk(disk=data["disk"], health=data.get("disk_health", {})):
            for i, (mount, d) in enumerate(sorted(disk.items())):
                plt.plot(d["t"], d["used"], label=_L(mount), color=_scolor(i), marker="braille")
            _title(f"disk used (%){_disk_tag(disk, health)}")
            _ylabel("%")
            _int_yticks(*[d["used"] for d in disk.values()])
        panels.append(draw_disk)
    if "thermal" in selected and data["thermal"]:
        def draw_thermal(zones=data["thermal"]):
            for i, (zone, d) in enumerate(sorted(zones.items())):
                plt.plot(d["t"], d["temp"], label=_L(zone), color=_scolor(i), marker="braille")
            _title("thermal zones (degC)")
            _ylabel("degC")
            _int_yticks(*[d["temp"] for d in zones.values()])
        panels.append(draw_thermal)
    if "power" in selected and data["power"]:
        def draw_power(rails=data["power"]):
            total = 0.0; n = 0
            for i, (rail, d) in enumerate(sorted(rails.items())):
                plt.plot(d["t"], d["watts"], label=_L(rail), color=_scolor(i), marker="braille")
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
    if "self" in selected and data.get("self"):
        def draw_self(d=data["self"]):
            plt.plot(d["t"], d["rss"], label=_L("rss MB"), color="magenta+", marker="braille")
            plt.plot(d["t"], d["cpu"], label=_L("cpu%"), color="orange+", marker="braille")
            rss = query.last_value(d["rss"])
            tag = f"   rss {rss:.0f} MB" if rss is not None else ""
            wr = query.last_value(d.get("write", []))
            if wr is not None:
                tag += f"   {wr:.0f} MB/day SD"
            _title(f"smokemon self{tag}")
            _ylabel("MB / %")
            _int_yticks(d["rss"], d["cpu"])
        panels.append(draw_self)
    return panels


MIN_PANEL_H = 4   # framed panel floor: title + top border + 1 data row + bottom border
MIN_COL_W = 32    # narrowest column plotext can still draw a readable braille panel in


def _grid_dims(n: int, cols_opt: int, term_cols: int, term_lines: int = 0) -> tuple[int, int]:
    """Auto-grid. Start from the width default (2 cols if terminal >= 140 chars and >=3
    panels, else 1) but, when the available height is known, add columns until every panel
    gets at least MIN_PANEL_H rows — otherwise tall panel sets overflow a short terminal and
    the bottom panels collapse into stray frame lines. Bounded by a readable column width.
    Explicit --cols N forces the count (still clamped to <= n)."""
    if n <= 0:
        return (0, 0)
    if cols_opt > 0:
        cols = min(cols_opt, n)
    else:
        cols = 2 if (term_cols >= 140 and n >= 3) else 1
        if term_lines:
            max_cols = max(1, min(n, term_cols // MIN_COL_W))
            while cols < max_cols and math.ceil(n / cols) * MIN_PANEL_H > term_lines:
                cols += 1
    rows = math.ceil(n / cols)
    return rows, cols


def run(opts, *, capture: bool = False):
    """Render the TUI once. With capture=True, return the frame as a string (or an
    error/empty message) instead of printing it, so the live loop can repaint in
    place without a screen-clear flicker."""
    global KIOSK, NOLEGEND, NOFRAME
    KIOSK = getattr(opts, "kiosk", False)
    NOLEGEND = getattr(opts, "no_legend", False)
    NOFRAME = getattr(opts, "no_frame", False)
    if not os.path.exists(opts.db):
        msg = f"No smokemon database at {opts.db}\n{query.COLLECT_HINT}"
        if capture:
            return msg
        print(msg, file=sys.stderr)
        return 1
    since, until = query.window(opts.hours, opts.minutes, opts.since, opts.until)
    return _render(opts, since, until, capture=capture)


def _render(opts, since, until, *, capture: bool = False):
    targets = [t.strip() for t in opts.targets.split(",")] if opts.targets else None
    sel = ALL_PANELS if opts.panels == "all" else [s.strip() for s in opts.panels.split(",")]
    node = opts.node
    conn = query.open_ro(opts.db)
    data = query.load_all(conn, since, until, targets, node, sel, ping_loader=query.load_ping_agg)
    conn.close()
    panels = _build(sel, data)
    if not panels:
        msg = ("No data in the selected window — widen it with --hours/--minutes, "
               "or check the collector is running.")
        if capture:
            return msg
        print(msg, file=sys.stderr)
        return 2
    dlo, dhi = _data_xspan(data)
    xlo, xhi = (dlo, dhi) if (dlo is not None and dhi is not None and dhi > dlo) else (since, until)
    ticks, labels = _ticks(xlo, xhi)
    cols_term, lines = shutil.get_terminal_size(fallback=(120, 40))
    # Always keep a 1-row/1-col safety margin (even in kiosk, reserve=0): plot lines must
    # never reach the terminal's last column (would wrap to a phantom row if autowrap leaks)
    # nor land content on the last row (writing the bottom-right cell scrolls the terminal).
    # Either desyncs the cursor-home repaint and the whole frame drifts on the next refresh.
    plotsize_h = max(10, lines - max(1, opts.reserve))
    rows, cols = _grid_dims(len(panels), getattr(opts, "cols", 0), cols_term, plotsize_h)
    # thin each series to ~2x the per-panel column width so plotext draws a line, not a smear.
    _downsample(data, max(80, (cols_term // cols) * 2))

    _reset_plotext()
    plt.theme("pro")
    plt.plotsize(max(20, cols_term - 1), plotsize_h)
    plt.subplots(rows, cols)
    for idx, draw in enumerate(panels):
        r = idx // cols + 1
        c = idx % cols + 1
        plt.subplot(r, c)
        plt.xlim(xlo, xhi)
        if KIOSK:
            plt.frame(True)
            plt.ticks_color(240)
            plt.xfrequency(0)
            plt.yfrequency(0)
        else:
            plt.xticks(ticks, labels)
        if NOFRAME:
            plt.frame(False)
            plt.grid(False, False)
        draw()
    # Blank any trailing grid cells (rows*cols > len(panels)): an undrawn subplot inherits
    # the previous cell's title, so the last panel's title gets repeated in every empty cell.
    for idx in range(len(panels), rows * cols):
        plt.subplot(idx // cols + 1, idx % cols + 1)
        plt.title("")
        plt.frame(False)
        plt.xfrequency(0)
        plt.yfrequency(0)
    if capture:
        return plt.build()
    plt.show()
    return 0


# ---------- S1: DVR scrubber ----------


def _replay_range(opts):
    """(full_since, full_until) for replay. A bare date scrubs that whole day; a
    datetime starts there and runs to now; otherwise the normal Nh/Nm window ending now."""
    w = getattr(opts, "window", None)
    if w:
        try:
            dt = datetime.fromisoformat(w)
            if len(w) <= 10:  # date only -> the whole day
                start = dt.timestamp()
                return start, start + 86400
            return dt.timestamp(), datetime.now().timestamp()
        except ValueError:
            from ..cli import _apply_window
            _apply_window(opts, w)
    return query.window(opts.hours, opts.minutes, opts.since, opts.until)


def _read_key(fd) -> str:
    """Blocking single keypress in raw mode; decodes arrow escape sequences to
    'left'/'right' and passes through plain chars."""
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":
        seq = os.read(fd, 2).decode(errors="ignore")
        return {"[C": "right", "[D": "left", "[A": "up", "[B": "down"}.get(seq, "esc")
    return ch


def replay(opts) -> int:
    """Replay a historical window like a tape deck: a sliding playhead frame the user
    scrubs with left/right (vim h/l), step with up/down, q to quit. Raw data is kept
    forever, so any past window is reachable. Requires a TTY."""
    global KIOSK
    KIOSK = False
    opts.reserve = 2
    if not os.path.exists(opts.db):
        print(f"No smokemon database at {opts.db}\n{query.COLLECT_HINT}", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("replay needs an interactive terminal (TTY).", file=sys.stderr)
        return 1
    import termios
    import tty

    full_since, full_until = _replay_range(opts)
    frame = max(60.0, opts.frame * 60.0)
    step = frame / 4.0
    head = full_until  # playhead = right edge of the visible frame; start at the end

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\033[?25l")
    try:
        tty.setraw(fd)
        while True:
            head = max(full_since + frame, min(full_until, head))
            since, until = head - frame, head
            os.write(1, b"\033[2J\033[H")
            pos = (head - full_since) / (full_until - full_since) if full_until > full_since else 1.0
            bar = "#" * int(pos * 30)
            print(f"REPLAY {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → "
                  f"{datetime.fromtimestamp(until):%H:%M}  [{bar:<30}]  "
                  f"←/→ scrub · ↑/↓ step · q quit\r")
            _render(opts, since, until)
            sys.stdout.write("\r")
            key = _read_key(fd)
            if key in ("q", "esc", "\x03"):
                break
            if key in ("right", "l"):
                head += step
            elif key in ("left", "h"):
                head -= step
            elif key == "up":
                step = min(frame, step * 2)
            elif key == "down":
                step = max(10.0, step / 2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\n")
    return 0
