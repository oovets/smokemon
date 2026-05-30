"""`smoke` CLI: one entry point with subcommands.
  smoke [tui]           static TUI (default)
  smoke live [window]   redraw TUI on an interval (default 15m, --refresh 10)
  smoke kiosk [window]  live + clean (no legend/axes/header; minimal panel title kept)
  smoke replay [when]   DVR scrubber over a historical window (arrow keys)
  smoke fleet [live]    aggregated view across all hub nodes (DB or --hub-url)
  smoke png             high-res PNG -> Preview
  smoke daily           dated PNG of the last 24h (for the scheduled job)
  smoke status          one-line sparkline health summary (QW3)
  smoke incidents       detected incidents + multi-signal blame (F1/F2)
  smoke digest          plain-english summary of the window (F3)
Window is Nh / Nm / bare-number (minutes). Common: --panels --minutes/--hours/
--since/--until --targets --node --db."""

import argparse
import os
import shutil
import sys
import time

from . import config

GRAPHS = os.path.join(config.HOME, "smokemon", "graphs")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=config.DB_PATH)
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--minutes", type=float)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--targets", help="comma-separated ping targets")
    p.add_argument("--panels", default="all", help=f"{','.join(config.PANELS)} or 'all'")
    p.add_argument("--node", help="filter to one node (required on a hub DB)")
    p.add_argument("--cols", type=int, default=0,
                   help="grid columns (0=auto: 2 cols if wide enough and >=3 panels)")


def _apply_window(args, win: str | None) -> str:
    """Set args.hours/minutes from a window token (Nh / Nm / bare minutes); return a label."""
    win = win or "15m"
    num = win[:-1] if win[-1:] in ("h", "m") else win
    try:
        val = float(num)
    except ValueError:
        sys.exit(f"smoke: invalid window {win!r} — use e.g. 24h, 90m, or 30 (minutes)")
    if win.endswith("h"):
        args.hours, args.minutes = val, None
        return f"{num}h"
    args.minutes = val
    return f"{num} min"


def _clip_visible(line: str, width: int) -> str:
    """Truncate an ANSI-colored line to at most `width` visible columns. plotext does not
    honour our requested plotsize exactly (braille needs an even width, and the subplot
    grid rounds each column up), so a "full-width" frame can still reach the terminal's
    last column. Writing that column risks a wrap into a phantom row, which desyncs the
    cursor-home repaint. Clip here so it can never happen, then reset SGR at the cut."""
    out, vis, i, n, had_sgr = [], 0, 0, len(line), False
    while i < n:
        ch = line[i]
        if ch == "\x1b" and i + 1 < n and line[i + 1] == "[":  # CSI escape -> copy verbatim
            j = i + 2
            while j < n and not line[j].isalpha():
                j += 1
            j += 1
            seq = line[i:j]
            out.append(seq)
            if seq.endswith("m"):
                had_sgr = True
            i = j
            continue
        if vis >= width:  # reached the column budget; drop the rest of the printable text
            i += 1
            continue
        out.append(ch)
        vis += 1
        i += 1
    if had_sgr:
        out.append("\x1b[0m")
    return "".join(out)


def _live(args, frame_fn, *, kiosk: bool, title: str, bell_fn=None) -> int:
    """Repaint loop shared by `smoke live/kiosk` and `smoke fleet live`. frame_fn() returns
    the frame text to draw; bell_fn(prev) (optional) handles --bell and returns the new
    state. The careful absolute-addressing repaint below is renderer-agnostic."""
    args.kiosk, args.reserve = kiosk, (0 if kiosk else 2)
    out = sys.stdout
    # Hide cursor, disable line-wrap (full-width plot lines must not wrap onto an extra
    # row — that desyncs the cursor-home repaint and ghosts rows), clear once.
    out.write("\033[?25l\033[?7l\033[2J")
    out.flush()
    prev_verdict = "healthy"
    try:
        while True:
            frame = frame_fn()
            if getattr(args, "bell", False) and bell_fn:
                prev_verdict = bell_fn(prev_verdict)
            cols, rows = shutil.get_terminal_size(fallback=(120, 40))
            lines = ([] if kiosk else
                     [f"smokemon LIVE — {title} · refresh {args.refresh}s · "
                      f"{time.strftime('%H:%M:%S')} · Ctrl-C to quit"]) + frame.split("\n")
            # Keep one row/column of slack: never write the terminal's last row (writing the
            # bottom-right cell scrolls it) nor its last column (risks a phantom-row wrap).
            lines = [_clip_visible(ln, cols - 1) for ln in lines[:rows - 1]]
            # Address every row absolutely (ESC[row;1H) instead of relying on "\n": with no
            # newlines emitted, the repaint can't drift no matter how the terminal handles
            # line-feeds, autowrap, or scrolling. Clear each line, then erase below the frame.
            payload = "".join(f"\033[{i + 1};1H\033[K{ln}" for i, ln in enumerate(lines))
            payload += f"\033[{len(lines) + 1};1H\033[J"
            out.write(payload)
            out.flush()
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\033[?7h\033[?25h\n")  # restore wrap + cursor
        out.flush()
    return 0


def _maybe_bell(args, out, prev_verdict: str) -> str:
    """X2 sonification: ring the terminal bell once on each transition into an unhealthy
    state (not every frame), so a kiosk audibly flags trouble. Returns the new verdict."""
    from . import query, report
    healthy = {"healthy", "recovered"}
    try:
        since, until = query.window(args.hours, args.minutes, args.since, args.until)
        conn = query.open_ro(args.db)
        try:
            v = report.verdict(conn, since, until, args.node)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - never let the bell break the live loop
        return prev_verdict
    if v not in healthy and v != prev_verdict:
        out.write("\a")
        out.flush()
    return v


def _text_report(cmd: str, args) -> int:
    """status / incidents / digest: stdlib-only text surfaces (no plotext/matplotlib),
    so they run on a node as well as the hub."""
    from . import query, report
    if not os.path.exists(args.db):
        print(f"No smokemon database at {args.db}\n{query.COLLECT_HINT}", file=sys.stderr)
        return 1
    since, until = query.window(args.hours, args.minutes, args.since, args.until)
    color = report.use_color()
    conn = query.open_ro(args.db)
    try:
        if cmd == "status":
            out = report.status_line(conn, since, until, args.node, color=color)
        elif cmd == "incidents":
            out = report.incidents_report(conn, since, until, args.node, color=color)
        else:
            out = report.digest(conn, since, until, args.node)
        print(out)
        if getattr(args, "notify", False):
            from . import notify
            n = notify.alert_from_db(conn, since, until, args.node)
            print(f"\n(notify: alerted on {n} incident(s))" if n else "\n(notify: nothing above threshold)")
    finally:
        conn.close()
    return 0


def _http_get_json(base_url: str, path: str) -> dict:
    """GET base_url + path and decode JSON. Used by `smoke fleet --hub-url` so the
    aggregated view works from any terminal against the hub's read-only /api, with no
    access to the hub DB file."""
    import json
    import urllib.request
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _fleet_data(args):
    """Fleet payload from the hub DB (default) or the hub's HTTP /api (--hub-url).
    --ranked selects the incident-based ranking; otherwise the latest-sample status."""
    if args.hub_url:
        if args.heatmap:
            return _http_get_json(args.hub_url, f"/api/heatmap?metric={args.metric}&hours={args.hours}")
        if args.ranked:
            return _http_get_json(args.hub_url, f"/api/fleet?hours={args.hours}")["fleet"]
        return _http_get_json(args.hub_url, "/api/fleet-status")
    from . import hubapi, query  # stdlib only — no plotext/matplotlib
    if not os.path.exists(args.db):
        raise FileNotFoundError(f"no hub DB at {args.db} (set --db or use --hub-url)")
    conn = query.open_ro(args.db)
    try:
        if args.heatmap:
            return hubapi.heatmap(conn, args.metric, args.hours)
        if args.ranked:
            return hubapi.fleet(conn, args.hours)
        return hubapi.fleet_status(conn, stale_after_s=args.stale_after)
    finally:
        conn.close()


def _fleet_render(args, color: bool) -> tuple[str, object]:
    from . import report
    data = _fleet_data(args)
    if args.heatmap:
        return report.fleet_heatmap_report(data, color=color), data
    if args.ranked:
        return report.fleet_ranked_report(data, args.hours, color=color), data
    return report.fleet_status_report(data, color=color), data


def _fleet(args) -> int:
    """`smoke fleet [live]`: aggregated view across every node reporting to the hub."""
    from . import report
    live = getattr(args, "mode", None) == "live"
    if not live:
        color = report.use_color(disable=args.no_color)
        try:
            text, _ = _fleet_render(args, color)
        except Exception as e:  # noqa: BLE001 - one-shot: report and exit non-zero
            print(f"smoke fleet: {e}", file=sys.stderr)
            return 1
        print(text)
        return 0

    last: dict = {}

    def frame() -> str:
        try:
            text, data = _fleet_render(args, color=report.use_color(disable=args.no_color))
            last["data"] = data
            return text
        except Exception as e:  # noqa: BLE001 - never let a transient error kill the loop
            return f"fleet error: {e}"

    def bell(prev: str) -> str:
        # Ring once on each transition into a bad fleet state (any node down or stale).
        c = (last.get("data") or {}).get("counts", {}) if not args.ranked else {}
        state = "bad" if (c.get("down", 0) + c.get("stale", 0)) else "ok"
        if state == "bad" and state != prev:
            sys.stdout.write("\a")
            sys.stdout.flush()
        return state

    return _live(args, frame, kiosk=False, title="fleet",
                 bell_fn=(bell if args.bell else None))


def _normalize_hub_url(host: str) -> str:
    """'host' / 'host:port' / a full URL -> a complete http://host:PORT/ingest URL."""
    from urllib.parse import urlsplit, urlunsplit
    h = host.strip()
    if "://" not in h:
        h = "http://" + h
    p = urlsplit(h)
    netloc = p.netloc if ":" in p.netloc else f"{p.netloc}:{config.HUB_PORT}"
    path = p.path if p.path not in ("", "/") else "/ingest"
    return urlunsplit((p.scheme, netloc, path, "", ""))


def _env_get(path: str, key: str) -> str | None:
    """Read KEY=value from a simple env file; None if absent or unreadable."""
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if s.startswith(key + "="):
                    return s[len(key) + 1:]
    except OSError:
        pass
    return None


def _env_set(path: str, key: str, val: str) -> tuple[bool, str]:
    """Replace or append KEY=val in the env file. Returns (ok, reason-if-not)."""
    try:
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []
        out = [ln for ln in lines if not ln.startswith(key + "=")]
        out.append(f"{key}={val}")
        with open(path, "w") as f:
            f.write("\n".join(out) + "\n")
        return True, ""
    except OSError as e:
        return False, e.__class__.__name__


def _hub_status(url: str) -> str:
    """Quick reachability of a hub ingest URL via its sibling GET /health."""
    if not url:
        return "no hub set — this node is local-only"
    base = url[:-len("/ingest")] if url.endswith("/ingest") else url
    try:
        ok = _http_get_json(base, "/health").get("ok")
        return "reachable" if ok else "responded, but not a smokemon hub"
    except Exception as e:  # noqa: BLE001
        return f"unreachable ({e.__class__.__name__})"


def _hub(args) -> int:
    """`smoke hub` shows where this node ships; `smoke hub HOST` repoints it by writing
    SMOKEMON_HUB_URL to the node env file (config.ENV_FILE). The Linux shipper re-reads
    that file on its next 60s run; macOS keeps the value in the launchd plist instead."""
    envf = config.ENV_FILE
    current = _env_get(envf, "SMOKEMON_HUB_URL")
    if current is None:
        current = config.HUB_URL  # fall back to this process's env

    if not args.host:
        shown = current or "(none — local-only)"
        if not current and os.path.exists(envf) and not os.access(envf, os.R_OK):
            shown = f"(can't read {envf} without sudo)"
        print(f"hub:    {shown}")
        if current:
            print(f"status: {_hub_status(current)}")
        print(f"config: {envf}")
        if not current:
            print("set it: smoke hub HUB-HOST")
        return 0

    url = _normalize_hub_url(args.host)
    print(f"hub -> {url}")
    if sys.platform == "darwin":
        plist = "~/Library/LaunchAgents/com.smokemon.shipper.plist"
        print(f"  macOS keeps SMOKEMON_HUB_URL in the launchd plist, not {envf}.")
        print(f"  set it there, then reload:  defaults write... or edit {plist}, then")
        print("    launchctl kickstart -k gui/$(id -u)/com.smokemon.shipper")
        return 0
    ok, why = _env_set(envf, "SMOKEMON_HUB_URL", url)
    if ok:
        print(f"  written to {envf} — {_hub_status(url)}")
        print("  applies on the next shipper run (<=60s); force now:")
        print("    sudo systemctl start smokemon-shipper.service")
    else:
        print(f"  can't write {envf} ({why}); run:")
        print(f"    sudo sed -i '/^SMOKEMON_HUB_URL=/d' {envf} && "
              f"echo 'SMOKEMON_HUB_URL={url}' | sudo tee -a {envf}")
    return 0


_EXAMPLES = """\
examples:
  smoke                          dashboard, last 6h
  smoke live 24h                 live view, redraws in place
  smoke status                   one-line health summary (great in a prompt/tmux)
  smoke incidents --hours 48     problems + likely cause over 2 days
  smoke fleet                    every node at once (on the hub)
  smoke fleet --hub-url http://hub:8765    the fleet over HTTP, from any terminal
  smoke hub HUB-HOST             repoint this node to a new hub (writes the env file)

colour: auto on a TTY; honours the NO_COLOR env var. full reference: INSTALL.md / QUICKSTART.md
"""


def main() -> int:
    ap = argparse.ArgumentParser(prog="smoke", description="smokemon viewer", epilog=_EXAMPLES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("tui", help="static TUI")
    _common(p); p.add_argument("--kiosk", action="store_true"); p.add_argument("--reserve", type=int, default=0)

    for name in ("live", "kiosk"):
        p = sub.add_parser(name, help=f"{name} TUI")
        _common(p); p.add_argument("window", nargs="?"); p.add_argument("--refresh", type=float, default=10)
        p.add_argument("--bell", action="store_true",
                       help="ring the terminal bell when health degrades (X2 sonification)")

    p = sub.add_parser("png", help="PNG to Preview")
    _common(p)
    p.add_argument("--out", default=os.path.join(GRAPHS, "smokemon.png"))
    p.add_argument("--dpi", type=int, default=96); p.add_argument("--width", type=float, default=0)
    p.add_argument("--theme", choices=["light", "dark"], default="light", help="dark palette for the hub GUI")
    p.add_argument("--no-title", action="store_true", help="omit the figure suptitle (hub GUI has its own)")
    p.add_argument("--meta", action="store_true",
                   help="pull per-panel titles off the image; emit them + positions on stderr (hub GUI tooltips)")
    p.add_argument("--no-open", action="store_true")

    p = sub.add_parser("daily", help="dated 24h PNG")
    _common(p)

    p = sub.add_parser("replay", help="DVR scrubber over a historical window")
    _common(p)
    p.add_argument("window", nargs="?", help="date (2026-05-20), datetime, or Nh/Nm window")
    p.add_argument("--frame", type=float, default=60.0, help="playhead width in minutes (default 60)")

    p = sub.add_parser("hub", help="show or set where this node ships (SMOKEMON_HUB_URL)")
    p.add_argument("host", nargs="?",
                   help="hub host / host:port / URL to ship to (omit to show the current target)")

    p = sub.add_parser("fleet", help="aggregated view across all hub nodes")
    p.add_argument("mode", nargs="?", choices=["live"], help="'live' repaints on an interval")
    p.add_argument("--db", default=config.HUB_DB, help="hub DB (default SMOKEMON_HUB_DB)")
    p.add_argument("--hub-url", help="read /api over HTTP instead of the DB (e.g. http://hub:8765)")
    p.add_argument("--hours", type=float, default=24.0, help="window for --ranked (default 24)")
    p.add_argument("--ranked", action="store_true",
                   help="incident-based ranking (uptime/downtime over --hours) vs latest-sample status")
    p.add_argument("--heatmap", action="store_true",
                   help="node x hour sparkline heatmap over --hours instead of the status table")
    p.add_argument("--metric", choices=["loss", "rtt"], default="loss", help="heatmap metric")
    p.add_argument("--stale-after", type=float, default=300.0,
                   help="seconds without a fresh sample before a node is 'stale'")
    p.add_argument("--refresh", type=float, default=5.0, help="live repaint interval, seconds")
    p.add_argument("--bell", action="store_true", help="ring when any node is down/stale (live)")
    p.add_argument("--no-color", action="store_true", help="plain text, no ANSI colour")

    for name, helptext in (("status", "one-line sparkline health summary"),
                           ("incidents", "detected incidents + blame"),
                           ("digest", "plain-english window summary")):
        p = sub.add_parser(name, help=helptext)
        _common(p)
        if name in ("incidents", "digest"):
            p.add_argument("--notify", action="store_true",
                           help="also push qualifying incidents to SMOKEMON_NOTIFY_URL (S4)")

    known = {"tui", "live", "kiosk", "replay", "fleet", "hub", "png", "daily",
             "status", "incidents", "digest"}
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        pass  # show the top-level help (subcommands + examples), not the tui subparser's
    elif not argv or argv[0] not in known:
        argv = ["tui", *argv]  # default subcommand, so `smoke` and `smoke --minutes 30` work
    args = ap.parse_args(argv)
    from . import report
    report.ASCII = not report.unicode_ok()   # ascii glyph fallback on non-utf8 terminals
    cmd = args.cmd
    if cmd == "hub":
        return _hub(args)
    if cmd == "fleet":
        return _fleet(args)
    if cmd in ("status", "incidents", "digest"):
        return _text_report(cmd, args)
    if cmd in ("tui", "live", "kiosk", "replay"):
        from .render import tui  # plotext only — never pulls in matplotlib
        if cmd == "tui":
            return tui.run(args)
        if cmd == "replay":
            return tui.replay(args)
        label = _apply_window(args, args.window)
        return _live(args, lambda: tui.run(args, capture=True), kiosk=(cmd == "kiosk"),
                     title=f"last {label}",
                     bell_fn=lambda prev: _maybe_bell(args, sys.stdout, prev))
    try:
        from .render import png  # matplotlib+numpy, hub-side only
    except ModuleNotFoundError as e:
        print(f"PNG rendering needs matplotlib+numpy (hub only): {e}", file=sys.stderr)
        return 1
    if cmd == "daily":
        tag = f"-{args.node}" if args.node else ""
        args.hours, args.dpi, args.width, args.no_open = 24.0, 96, 0, True
        args.out = os.path.join(GRAPHS, "daily", f"smokemon{tag}-{time.strftime('%F')}.png")
    return png.run(args)


if __name__ == "__main__":
    sys.exit(main())
