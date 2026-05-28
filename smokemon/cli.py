"""`smoke` CLI: one entry point with subcommands.
  smoke [tui]           static TUI (default)
  smoke live [window]   redraw TUI on an interval (default 15m, --refresh 10)
  smoke kiosk [window]  live + clean (no legend/axes/title/header)
  smoke png             high-res PNG -> Preview
  smoke daily           dated PNG of the last 24h (for the scheduled job)
Window is Nh / Nm / bare-number (minutes). Common: --panels --minutes/--hours/
--since/--until --targets --node --db."""

import argparse
import os
import sys
import time

from . import config
from .render import png, tui

GRAPHS = os.path.join(config.HOME, "smokemon", "graphs")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=config.DB_PATH)
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--minutes", type=float)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--targets", help="comma-separated ping targets")
    p.add_argument("--panels", default="all", help=f"{','.join(tui.ALL_PANELS)} or 'all'")
    p.add_argument("--node", help="filter to one node (required on a hub DB)")


def _apply_window(args, win: str | None) -> str:
    """Set args.hours/minutes from a window token; return a display label."""
    win = win or "15m"
    if win.endswith("h"):
        args.hours, args.minutes = float(win[:-1]), None
        return f"{win[:-1]}h"
    val = win[:-1] if win.endswith("m") else win
    args.minutes = float(val)
    return f"{val} min"


def _live(args, kiosk: bool) -> int:
    label = _apply_window(args, args.window)
    args.kiosk, args.reserve = kiosk, (0 if kiosk else 2)
    sys.stdout.write("\033[?25l")  # hide cursor
    try:
        while True:
            os.system("clear")
            if not kiosk:
                print(f"smokemon LIVE — last {label} · refresh {args.refresh}s · "
                      f"{time.strftime('%H:%M:%S')} · Ctrl-C to quit")
            tui.run(args)
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")  # restore cursor
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="smoke", description="smokemon viewer")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("tui", help="static TUI")
    _common(p); p.add_argument("--kiosk", action="store_true"); p.add_argument("--reserve", type=int, default=0)

    for name in ("live", "kiosk"):
        p = sub.add_parser(name, help=f"{name} TUI")
        _common(p); p.add_argument("window", nargs="?"); p.add_argument("--refresh", type=float, default=10)

    p = sub.add_parser("png", help="PNG to Preview")
    _common(p)
    p.add_argument("--out", default=os.path.join(GRAPHS, "smokemon.png"))
    p.add_argument("--dpi", type=int, default=96); p.add_argument("--width", type=float, default=0)
    p.add_argument("--no-open", action="store_true")

    p = sub.add_parser("daily", help="dated 24h PNG")
    _common(p)

    argv = sys.argv[1:]
    if not argv or argv[0] not in {"tui", "live", "kiosk", "png", "daily"}:
        argv = ["tui", *argv]  # default subcommand, so `smoke` and `smoke --minutes 30` work
    args = ap.parse_args(argv)
    cmd = args.cmd
    if cmd == "tui":
        return tui.run(args)
    if cmd == "live":
        return _live(args, kiosk=False)
    if cmd == "kiosk":
        return _live(args, kiosk=True)
    if cmd == "png":
        return png.run(args)
    if cmd == "daily":
        tag = f"-{args.node}" if args.node else ""
        args.hours, args.dpi, args.width, args.no_open = 24.0, 96, 0, True
        args.out = os.path.join(GRAPHS, "daily", f"smokemon{tag}-{time.strftime('%F')}.png")
        return png.run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
