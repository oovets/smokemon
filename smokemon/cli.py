"""`smoke` CLI: one entry point with subcommands.
  smoke fleet [live]    one line per node: liveness + open incidents (DB or --hub-url)
  smoke fleet --heatmap node x hour incident-density grid
  smoke incidents       incident feed: what broke, where, still broken?
  smoke incident UID    one incident in full, with its captured evidence
  smoke status          one-line health summary (open incidents + heartbeat)
  smoke digest          plain-english summary of the window
  smoke footprint       collector rows/day + ship bytes/day estimate
  smoke hub [HOST ...]  show or set where this node ships
Common: --minutes/--hours/--since/--until --node --db."""

import argparse
import os
import shutil
import sys
import time

from . import config, ship


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=config.DB_PATH)
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--minutes", type=float)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--node", help="filter to one node (required on a hub DB)")


def _clip_visible(line: str, width: int) -> str:
    """Truncate an ANSI-colored line to at most `width` visible columns. A full-width frame
    can otherwise reach the terminal's last column, and writing that column risks a wrap into
    a phantom row, which desyncs the cursor-home repaint. Clip so it can never happen, then
    reset SGR at the cut."""
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
    """Repaint loop behind `smoke fleet live`. frame_fn() returns the frame text to draw;
    bell_fn(prev) (optional) handles --bell and returns the new state."""
    args.kiosk, args.reserve = kiosk, (0 if kiosk else 2)
    out = sys.stdout
    # Hide cursor, disable line-wrap (full-width lines must not wrap onto an extra row —
    # that desyncs the cursor-home repaint and ghosts rows), clear once.
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


def _text_report(cmd: str, args) -> int:
    """status / digest: stdlib-only text surfaces, so they run on a node as well as the hub."""
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


def _footprint(args) -> int:
    """`smoke footprint`: local collector production and shipper wire estimate."""
    from . import footprint, query
    if not os.path.exists(args.db):
        print(f"No smokemon database at {args.db}\n{query.COLLECT_HINT}", file=sys.stderr)
        return 1
    since, until = query.window(args.hours, args.minutes, args.since, args.until)
    conn = query.open_ro(args.db)
    try:
        fp = footprint.analyze(conn, args.db, since, until, args.node)
    finally:
        conn.close()
    print(footprint.render(fp, limit=args.limit))
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


def _resolve_db(args) -> None:
    """Pick the DB for the incident views when the caller did not name one.

    hubapi's incident readers work against a node DB as well as a hub DB -- both hold the same
    `incidents` table -- so the same command serves both roles. Preferring the hub DB when it
    exists means running `smoke incidents` on the hub shows the fleet rather than only the hub
    host's own incidents, which is the reading almost everyone means."""
    if args.db or args.hub_url:
        return
    args.db = config.HUB_DB if os.path.exists(config.HUB_DB) else config.DB_PATH


def _hub_read(args, path: str, call):
    """Run a hub read either over HTTP (--hub-url) or straight off the hub DB.

    Both paths must produce the same shape, because the renderers are shared: the HTTP
    endpoints are thin wrappers around exactly these hubapi functions."""
    if args.hub_url:
        return _http_get_json(args.hub_url, path)
    from . import query  # stdlib only — no plotting library on the read path
    if not os.path.exists(args.db):
        raise FileNotFoundError(f"no hub DB at {args.db} (set --db or use --hub-url)")
    conn = query.open_ro(args.db)
    try:
        return call(conn)
    finally:
        conn.close()


def _fleet_data(args):
    """`smoke fleet`: the node table, or the incident-density grid with --heatmap."""
    from . import hubapi
    if args.heatmap:
        return _hub_read(args, f"/api/density?hours={args.hours}",
                         lambda c: hubapi.incident_density(c, args.hours))
    # /api/fleet wraps the list in {"fleet": [...]} for the dashboard; unwrap so both
    # transports hand the renderer the same list.
    data = _hub_read(args, "/api/fleet", hubapi.fleet)
    return data["fleet"] if isinstance(data, dict) else data


def _fleet_render(args, color: bool) -> tuple[str, object]:
    from . import report
    data = _fleet_data(args)
    if args.heatmap:
        return report.density_report(data, color=color), data
    return report.fleet_report(data, color=color), data


def _incidents(args) -> int:
    """`smoke incidents`: the hub-wide incident feed."""
    from urllib.parse import quote

    from . import hubapi, report
    _resolve_db(args)
    q = f"/api/incidents?hours={args.hours}" + (f"&node={quote(args.node)}" if args.node else "")
    try:
        feed = _hub_read(args, q, lambda c: hubapi.incidents_feed(c, args.hours, args.node))
    except Exception as e:  # noqa: BLE001 - one-shot: report and exit non-zero
        print(f"smoke incidents: {e}", file=sys.stderr)
        return 1
    print(report.incidents_feed_report(feed, color=report.use_color(disable=args.no_color)))
    return 0


def _incident(args) -> int:
    """`smoke incident UID`: one incident with its captured evidence."""
    from urllib.parse import quote

    from . import hubapi, report
    _resolve_db(args)
    try:
        inc = _hub_read(args, f"/api/incident?uid={quote(args.uid)}",
                        lambda c: hubapi.incident_detail(c, args.uid))
    except Exception as e:  # noqa: BLE001
        print(f"smoke incident: {e}", file=sys.stderr)
        return 1
    if not inc or inc.get("error"):
        print(f"no incident with uid {args.uid}", file=sys.stderr)
        return 1
    print(report.incident_detail_report(_reconcile_state(args, inc),
                                        color=report.use_color(disable=args.no_color)))
    return 0


def _reconcile_state(args, inc: dict) -> dict:
    """Apply the silent-node rule to a single incident.

    hubapi.incidents_feed downgrades an open incident on a node we can no longer hear from to
    `unknown`, but incident_detail returns the raw reduction and does not. Rendering that as
    `ongoing` would have the detail view assert as fact the very thing the feed refuses to
    guess about -- and the detail view is the one an operator reads before deciding whether to
    drive to site."""
    if inc.get("state") != "ongoing":
        return inc
    fleet = _fleet_data(_ns(args, heatmap=False))
    live = {r["node"]: r.get("liveness") for r in fleet}
    if live.get(inc["node"]) == "live":
        return inc
    return {**inc, "state": "unknown", "unknown_reason": "node silent"}


def _ns(args, **over):
    """A copy of `args` with fields overridden, so a handler can reuse another command's
    data-fetch path without mutating the namespace argparse handed it."""
    from types import SimpleNamespace
    return SimpleNamespace(**{**vars(args), **over})


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
        # Ring once on each transition into a bad fleet state. `unknown` counts: a node that
        # went silent mid-incident is exactly the case a human needs to look at.
        rows = last.get("data") or []
        bad = {"dead", "unknown", "critical"}
        state = "bad" if any(r.get("state") in bad for r in rows
                             if isinstance(r, dict)) else "ok"
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


def _env_unset(path: str, key: str) -> None:
    """Remove any KEY=... lines from the env file. No-op if the file or key is absent. Used to
    keep SMOKEMON_HUB_URL and SMOKEMON_HUB_URLS from coexisting (the list would shadow the single
    var per config._hubs), so `smoke hub` writes exactly one of them."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    out = [ln for ln in lines if not ln.startswith(key + "=")]
    if len(out) != len(lines):
        try:
            with open(path, "w") as f:
                f.write("\n".join(out) + ("\n" if out else ""))
        except OSError:
            pass


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


def _current_hubs(envf: str) -> list[str]:
    """The node's configured hub URLs, mirroring config._hubs precedence: the semicolon list
    SMOKEMON_HUB_URLS wins over the single SMOKEMON_HUB_URL; both read from the env file, then
    this process's resolved config as a fallback."""
    urls_val = _env_get(envf, "SMOKEMON_HUB_URLS")
    if urls_val:
        return [u.strip() for u in urls_val.split(";") if u.strip()]
    single = _env_get(envf, "SMOKEMON_HUB_URL")
    if single:
        return [single]
    return [u for u, _ in config.HUBS]


def _hub(args) -> int:
    """`smoke hub` shows where this node ships; `smoke hub HOST [HOST2 ...]` repoints it by writing
    the node env file (config.ENV_FILE). One host writes SMOKEMON_HUB_URL; several write the
    semicolon list SMOKEMON_HUB_URLS (fan-out: every hub gets a full copy). Whichever form is
    written, the other var is cleared so it can't shadow it. The collector re-reads the file on
    SIGHUP; without SIGHUP the change applies on the next process restart."""
    envf = config.ENV_FILE
    current = _current_hubs(envf)

    if not args.hosts:
        if not current:
            shown = "(none — local-only)"
            if os.path.exists(envf) and not os.access(envf, os.R_OK):
                shown = f"(can't read {envf} without sudo)"
            print(f"hub:    {shown}")
            print(f"config: {envf}")
            print("set it: smoke hub HUB-HOST   (fan-out to several: smoke hub HUB-A HUB-B)")
            return 0
        if len(current) == 1:
            print(f"hub:    {current[0]}")
            print(f"status: {_hub_status(current[0])}")
        else:
            print(f"hubs:   {len(current)} targets (fan-out — each gets a full copy)")
            for u in current:
                print(f"  {u}  — {_hub_status(u)}")
        print(f"config: {envf}")
        return 0

    urls, seen = [], set()
    for h in args.hosts:  # normalize + de-dup, preserving order
        u = _normalize_hub_url(h)
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for u in urls:
        ok, why = ship.hub_url_ok(u)
        if not ok:
            print(f"hub: warning: {u} will be ignored by the shipper unless transport is safe: {why}",
                  file=sys.stderr)

    if len(urls) == 1:
        print(f"hub -> {urls[0]}")
    else:
        print(f"hubs -> {len(urls)} targets (fan-out):")
        for u in urls:
            print(f"  {u}")

    if len(urls) == 1:
        ok, why = _env_set(envf, "SMOKEMON_HUB_URL", urls[0])
        if ok:
            _env_unset(envf, "SMOKEMON_HUB_URLS")  # a stale list would otherwise shadow this
        status = _hub_status(urls[0])
    else:
        ok, why = _env_set(envf, "SMOKEMON_HUB_URLS", ";".join(urls))
        if ok:
            _env_unset(envf, "SMOKEMON_HUB_URL")   # the list supersedes the single var
        status = ", ".join(f"{u} {_hub_status(u)}" for u in urls)
    if ok:
        print(f"  written to {envf} — {status}")
        print("  applies after SIGHUP or process restart; force now:")
        print("    pkill -HUP smokemon      (or sudo systemctl reload smokemon)")
    else:
        print(f"  can't write {envf} ({why}); edit it with sudo (set the var shown above).")
    return 0


_EXAMPLES = """\
examples:
  smoke                          one-line health summary, last 6h
  smoke status                   the same, explicitly (great in a prompt/tmux)
  smoke incidents --hours 48     what broke across the fleet over 2 days
  smoke incident a1b2c3d4e5f6    one incident with its captured samples + log excerpt
  smoke fleet                    every node at once (on the hub)
  smoke fleet --heatmap          node x hour incident density, last 7 days
  smoke footprint                collector footprint + ship estimate
  smoke fleet --hub-url http://hub:8765    the fleet over HTTP, from any terminal
  smoke hub HUB-HOST             repoint this node to a new hub (writes the env file)
  smoke hub HUB-A HUB-B          fan out to several hubs (each gets a full copy)

colour: auto on a TTY; honours the NO_COLOR env var. full reference: INSTALL.md / QUICKSTART.md
"""


def main() -> int:
    ap = argparse.ArgumentParser(prog="smoke", description="smokemon viewer", epilog=_EXAMPLES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("hub", help="show or set where this node ships (one hub, or several for fan-out)")
    p.add_argument("hosts", nargs="*",
                   help="hub host(s) / host:port / URL to ship to; give several for fan-out "
                        "(each hub gets a full copy). Omit to show the current target(s).")

    p = sub.add_parser("fleet", help="aggregated view across all hub nodes")
    p.add_argument("mode", nargs="?", choices=["live"], help="'live' repaints on an interval")
    p.add_argument("--db", default=config.HUB_DB, help="hub DB (default SMOKEMON_HUB_DB)")
    p.add_argument("--hub-url", help="read /api over HTTP instead of the DB (e.g. http://hub:8765)")
    p.add_argument("--hours", type=float, default=168.0, help="window for --heatmap (default 168)")
    p.add_argument("--heatmap", action="store_true",
                   help="node x hour incident-density grid over --hours instead of the node table")
    p.add_argument("--refresh", type=float, default=5.0, help="live repaint interval, seconds")
    p.add_argument("--bell", action="store_true", help="ring when any node is down/stale (live)")
    p.add_argument("--no-color", action="store_true", help="plain text, no ANSI colour")

    p = sub.add_parser("incidents", help="incident feed: what broke, where, still broken?")
    p.add_argument("--db", help="incident DB (default: the hub DB if present, else the node DB)")
    p.add_argument("--hub-url", help="read /api over HTTP instead of the DB (e.g. http://hub:8765)")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--node", help="filter to one node")
    p.add_argument("--no-color", action="store_true", help="plain text, no ANSI colour")

    p = sub.add_parser("incident", help="one incident in full, with its captured evidence")
    p.add_argument("uid", help="incident uid (from `smoke incidents`)")
    p.add_argument("--db", help="incident DB (default: the hub DB if present, else the node DB)")
    p.add_argument("--hub-url", help="read /api over HTTP instead of the DB")
    p.add_argument("--no-color", action="store_true", help="plain text, no ANSI colour")

    for name, helptext in (("status", "one-line health summary"),
                           ("digest", "plain-english window summary")):
        p = sub.add_parser(name, help=helptext)
        _common(p)
        if name == "digest":
            p.add_argument("--notify", action="store_true",
                           help="also push qualifying incidents to SMOKEMON_NOTIFY_URL (S4)")

    p = sub.add_parser("footprint", help="collector rows/day + ship bytes/day estimate")
    p.add_argument("--db", default=config.DB_PATH)
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--minutes", type=float)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--node", help="filter to one node (useful on a hub DB)")
    p.add_argument("--limit", type=int, default=8, help="number of top tables to show")

    known = {"fleet", "hub", "status", "incidents", "incident", "digest", "footprint"}
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        pass  # show the top-level help (subcommands + examples)
    elif not argv or argv[0] not in known:
        argv = ["status", *argv]  # default subcommand, so `smoke` and `smoke --minutes 30` work
    args = ap.parse_args(argv)
    from . import report
    report.ASCII = not report.unicode_ok()   # ascii glyph fallback on non-utf8 terminals
    cmd = args.cmd
    if cmd == "hub":
        return _hub(args)
    if cmd == "fleet":
        return _fleet(args)
    if cmd == "incidents":
        return _incidents(args)
    if cmd == "incident":
        return _incident(args)
    if cmd == "footprint":
        return _footprint(args)
    return _text_report(cmd, args)


if __name__ == "__main__":
    sys.exit(main())
