"""`smoke` CLI: one entry point with subcommands.
  smoke [tui]           static TUI (default)
  smoke live [window]   redraw TUI on an interval (default 15m, --refresh 10)
  smoke kiosk [window]  live + clean (no legend/axes/title/header)
  smoke replay [when]   DVR scrubber over a historical window (arrow keys)
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


def _live(args, tui, kiosk: bool) -> int:
    label = _apply_window(args, args.window)
    args.kiosk, args.reserve = kiosk, (0 if kiosk else 2)
    out = sys.stdout
    # Hide cursor, disable line-wrap (full-width plot lines must not wrap onto an extra
    # row — that desyncs the cursor-home repaint and ghosts rows), clear once.
    out.write("\033[?25l\033[?7l\033[2J")
    out.flush()
    prev_verdict = "healthy"
    try:
        while True:
            frame = tui.run(args, capture=True)
            if getattr(args, "bell", False):
                prev_verdict = _maybe_bell(args, out, prev_verdict)
            rows = shutil.get_terminal_size(fallback=(120, 40)).lines
            lines = ([] if kiosk else
                     [f"smokemon LIVE — last {label} · refresh {args.refresh}s · "
                      f"{time.strftime('%H:%M:%S')} · Ctrl-C to quit"]) + frame.split("\n")
            lines = lines[:rows]  # never write more rows than fit -> no scroll/ghosting
            # Home, repaint each line with erase-to-eol, then erase-below: overwrites the
            # previous frame in place (no clear = no flash) with no leftover/ghost rows.
            out.write("\033[H" + "\033[K\n".join(lines) + "\033[K\033[J")
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
        print(f"No database found: {args.db}", file=sys.stderr)
        return 1
    since, until = query.window(args.hours, args.minutes, args.since, args.until)
    conn = query.open_ro(args.db)
    try:
        if cmd == "status":
            out = report.status_line(conn, since, until, args.node)
        elif cmd == "incidents":
            out = report.incidents_report(conn, since, until, args.node)
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


def main() -> int:
    ap = argparse.ArgumentParser(prog="smoke", description="smokemon viewer")
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
    p.add_argument("--no-open", action="store_true")

    p = sub.add_parser("daily", help="dated 24h PNG")
    _common(p)

    p = sub.add_parser("replay", help="DVR scrubber over a historical window")
    _common(p)
    p.add_argument("window", nargs="?", help="date (2026-05-20), datetime, or Nh/Nm window")
    p.add_argument("--frame", type=float, default=60.0, help="playhead width in minutes (default 60)")

    for name, helptext in (("status", "one-line sparkline health summary"),
                           ("incidents", "detected incidents + blame"),
                           ("digest", "plain-english window summary")):
        p = sub.add_parser(name, help=helptext)
        _common(p)
        if name in ("incidents", "digest"):
            p.add_argument("--notify", action="store_true",
                           help="also push qualifying incidents to SMOKEMON_NOTIFY_URL (S4)")

    known = {"tui", "live", "kiosk", "replay", "png", "daily", "status", "incidents", "digest"}
    argv = sys.argv[1:]
    if not argv or argv[0] not in known:
        argv = ["tui", *argv]  # default subcommand, so `smoke` and `smoke --minutes 30` work
    args = ap.parse_args(argv)
    cmd = args.cmd
    if cmd in ("status", "incidents", "digest"):
        return _text_report(cmd, args)
    if cmd in ("tui", "live", "kiosk", "replay"):
        from .render import tui  # plotext only — never pulls in matplotlib
        if cmd == "tui":
            return tui.run(args)
        if cmd == "replay":
            return tui.replay(args)
        return _live(args, tui, kiosk=(cmd == "kiosk"))
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
