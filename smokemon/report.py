"""Text surfaces over the incident store: a one-line status, an incident table and a
plain-english digest, plus the terminal fleet views. All stdlib and renderer-free, so they
run on a node as well as the hub."""

import os
import sys
from datetime import datetime

from . import analyze, config, query

_SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_SPARK_ASCII = ".:-=+*#@"   # ascii magnitude ramp for terminals that can't render the blocks
DIGEST_MAX_INCIDENTS = 10   # cap the digest's detail list; full list via `smoke incidents`

# Terminal-capability flags. The CLI flips ASCII on when stdout can't render unicode;
# tests and library callers keep the default (unicode, colour decided per-call).
ASCII = False

# Colour per stored severity word. info stays uncoloured so a wall of routine transitions
# does not read as alarming.
_SEV_SGR = {"crit": "31", "error": "31", "warn": "33"}


def unicode_ok(stream=None) -> bool:
    """True when the stream is UTF-8 (so block sparklines / state dots render)."""
    enc = getattr(stream or sys.stdout, "encoding", "") or ""
    return "utf" in enc.lower()


def use_color(stream=None, *, disable: bool = False) -> bool:
    """True when ANSI colour is appropriate: a tty, NO_COLOR unset, not explicitly off.
    Follows the no-color.org convention - NO_COLOR present (any value) -> never colour."""
    if disable or "NO_COLOR" in os.environ:
        return False
    return bool(getattr(stream or sys.stdout, "isatty", lambda: False)())


def _sgr(s: str, code: str, color: bool) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if color else s


def _ramp() -> str:
    return _SPARK_ASCII if ASCII else _SPARK


def sparkline(vals, lo=None, hi=None) -> str:
    """Unicode block sparkline of a numeric series. None/NaN render as a space so gaps
    stay visible. lo/hi can be pinned (e.g. 0..100 for a percentage) else autoscaled."""
    finite = [v for v in vals if v is not None and v == v]
    if not finite:
        return ""
    lo = min(finite) if lo is None else lo
    hi = max(finite) if hi is None else hi
    span = (hi - lo) or 1.0
    chars = _ramp()
    out = []
    for v in vals:
        if v is None or v != v:
            out.append(" ")
            continue
        frac = (v - lo) / span
        idx = max(0, min(len(chars) - 1, int(frac * (len(chars) - 1) + 0.5)))
        out.append(chars[idx])
    return "".join(out)


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _stamp(ts) -> str:
    """Date + time. The fleet views span days, where a bare HH:MM is genuinely ambiguous about
    which day an incident opened on."""
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _label(inc: dict) -> str:
    """'signal entity' for an incident, or just the signal when it has no entity."""
    return f"{inc['signal']} {inc['entity']}" if inc.get("entity") else inc["signal"]


def _worst(incidents: list[dict]) -> dict | None:
    return max(incidents, key=lambda i: analyze.severity_rank(i.get("severity")), default=None)


# ---------- one-line status ----------


def status_line(conn, since, until, node=None, *, color: bool = False) -> str:
    """One glanceable row: what is open right now, plus the node's last heartbeat. Silence is
    ambiguous without the heartbeat - a node with nothing to say looks exactly like a dead one -
    so the age is always shown, even when no incident is open."""
    incidents = query.load_incidents(conn, since, until, node)
    ongoing = [i for i in incidents if i["state"] == "ongoing"]
    parts = []
    if ongoing:
        worst = _worst(ongoing)
        more = f" (+{len(ongoing) - 1} more)" if len(ongoing) > 1 else ""
        parts.append(_sgr(f"{worst['severity']} {_label(worst)}{more}",
                          _SEV_SGR.get(worst["severity"], "0"), color))
    elif incidents:
        parts.append(_sgr("recovered", "33", color))
    else:
        parts.append(_sgr("healthy", "32", color))

    hb = query.latest_heartbeat(conn, node or config.NODE)
    if hb:
        parts.append(f"heartbeat {_fmt_age(until - hb['ts'])} ago")
        if hb.get("cpu_pct") is not None:
            parts.append(f"cpu {hb['cpu_pct']:.0f}%")
        if hb.get("temp_c") is not None:
            parts.append(f"{hb['temp_c']:.0f}C")
    else:
        parts.append("no heartbeat")
    return " \u00b7 ".join(parts)


# ---------- incident table ----------


def incidents_report(conn, since, until, node=None, *, color: bool = False) -> str:
    """Incident table over the window, newest first. Read-only."""
    incidents = query.load_incidents(conn, since, until, node)
    span_h = (until - since) / 3600
    head = f"{len(incidents)} incident(s) in the last {span_h:.1f}h"
    if node:
        head = f"[{node}] " + head
    if not incidents:
        return head + " \u2014 all clear."
    lines = [head + ":", ""]
    for i in incidents:
        sev = _sgr(f"{(i['severity'] or '?'):<5}", _SEV_SGR.get(i["severity"], "0"), color)
        end = _hhmm(i["ended_ts"]) if i["ended_ts"] else "  now"
        dur = analyze._dur(i["duration_s"]) if i["duration_s"] is not None else "ongoing"
        worst = f" worst {i['worst_value']:.1f}" if i["worst_value"] is not None else ""
        lines.append(f"[{_hhmm(i['opened_ts'])}-{end}] {sev} {_label(i):<24} {dur:>7}{worst}")
    return "\n".join(lines)


# ---------- plain-english digest ----------


def digest(conn, since, until, node=None) -> str:
    """Narrative summary of the window: incident counts by severity, the longest ones, and
    the slow trends only the heartbeat carries (disk headroom, SD wear, the agent's own DB)."""
    incidents = query.load_incidents(conn, since, until, node)
    span_h = (until - since) / 3600
    name = node or config.NODE
    title = (f"smokemon digest \u2014 {name} \u2014 "
             f"{datetime.fromtimestamp(since):%Y-%m-%d %H:%M} \u2192 "
             f"{datetime.fromtimestamp(until):%H:%M}  ({span_h:.1f}h)")
    lines = [title, "=" * len(title), ""]

    if incidents:
        by_sev: dict[str, int] = {}
        for i in incidents:
            by_sev[i["severity"] or "?"] = by_sev.get(i["severity"] or "?", 0) + 1
        breakdown = ", ".join(f"{n} {s}" for s, n in sorted(
            by_sev.items(), key=lambda kv: -analyze.severity_rank(kv[0])))
        lines.append(f"{len(incidents)} incident(s): {breakdown}.")
        ongoing = [i for i in incidents if i["state"] == "ongoing"]
        if ongoing:
            lines.append(f"Still open: {', '.join(_label(i) for i in ongoing)}.")
        # Union the spans so two signals tripping on one root cause count as one bad stretch
        # rather than twice the time.
        spans = analyze.merge_spans([(i["opened_ts"], i["ended_ts"] or until) for i in incidents])
        lines.append(f"Time in incident: {analyze._dur(sum(e - s for s, e in spans))} "
                     f"across {len(spans)} stretch(es).")
    else:
        lines.append("No incidents detected.")

    hb = query.latest_heartbeat(conn, name)
    if hb is None:
        lines.append("No heartbeat \u2014 this node has never reported.")
    else:
        lines.append(f"Last heartbeat: {_fmt_age(until - hb['ts'])} ago"
                     + (f", agent up {analyze._dur(hb['agent_uptime_s'])}"
                        if hb.get("agent_uptime_s") is not None else "") + ".")
        if hb.get("disk_used_pct") is not None:
            free = f", {hb['disk_free_gb']:.1f} GB free" if hb.get("disk_free_gb") is not None else ""
            lines.append(f"Disk: {hb['disk_used_pct']:.0f}% used{free}.")
        if hb.get("wear_pct") is not None:
            lines.append(f"SD wear: {hb['wear_pct']:.0f}%.")
        if hb.get("db_mb") is not None:
            wal = f" (+{hb['wal_mb']:.1f} MB WAL)" if hb.get("wal_mb") is not None else ""
            lines.append(f"Own database: {hb['db_mb']:.1f} MB{wal}.")
        if hb.get("signal_drops"):
            # A non-zero drop count means the detector shed signals it could not keep up with,
            # so any "all clear" above is only as complete as what survived.
            lines.append(f"Detector dropped {hb['signal_drops']} signal(s) \u2014 coverage was incomplete.")

    ext = query.load_ext_events(conn, since, until, node, limit=5)
    if ext:
        lines.append("External events: " + "; ".join(
            f"{_hhmm(e['ts'])} {e['source']} {e['event']}" for e in ext))

    if incidents:
        ranked = sorted(incidents, key=lambda i: (analyze.severity_rank(i.get("severity")),
                                                  i["duration_s"] or 0.0), reverse=True)
        top = ranked[:DIGEST_MAX_INCIDENTS]
        lines += ["", f"Top incidents (of {len(incidents)}):"]
        for i in top:
            dur = analyze._dur(i["duration_s"]) if i["duration_s"] is not None else "ongoing"
            lines.append(f"  - [{_hhmm(i['opened_ts'])}] {i['severity']} {_label(i)}: {dur}")
        if len(incidents) > len(top):
            lines.append(f"  \u2026 {len(incidents) - len(top)} more (run `smoke incidents`).")
    return "\n".join(lines)


# ---------- fleet: aggregated terminal view across all hub nodes ----------
#
# Renders what hubapi.fleet() / incident_density() / incidents_feed() / incident_detail()
# return, whether read from the hub DB directly or fetched as JSON from the matching /api
# endpoint. Stdlib and renderer-free like the other report surfaces, so `smoke fleet` needs
# no plotting library.

# The fleet states hubapi.fleet() emits. `dead` and `unknown` are deliberately not folded
# into one bucket: one means the heartbeat aged out, the other that we have never had one.
_STATE_SGR = {"dead": "31", "unknown": "35", "critical": "31",
              "degraded": "33", "stale": "90", "ok": "32"}
_STATE_DOT = {"dead": "●", "unknown": "?", "critical": "●",
              "degraded": "●", "stale": "○", "ok": "●"}
_STATE_DOT_ASCII = {"dead": "x", "unknown": "?", "critical": "!",
                    "degraded": "!", "stale": ".", "ok": "+"}


def _dot(state: str, color: bool) -> str:
    glyph = (_STATE_DOT_ASCII if ASCII else _STATE_DOT).get(state, "?")
    return _sgr(glyph, _STATE_SGR.get(state, "0"), color)


def _fmt_age(a) -> str:
    """Compact relative age, matching the dashboard's fmtAge (s < 90, m < 90, else h)."""
    if a is None:
        return "?"
    if a < 90:
        return f"{a:.0f}s"
    if a < 5400:
        return f"{a / 60:.0f}m"
    return f"{a / 3600:.0f}h"


def fleet_report(fleet: list, *, color: bool = True) -> str:
    """One line per node from hubapi.fleet(): liveness, open incidents and the two heartbeat
    fields that most often explain a node going bad on its own (disk headroom, temperature).

    The API already sorts worst-first and that order is load-bearing -- it encodes the
    dead/unknown/critical precedence -- so this never re-sorts."""
    head = f"FLEET \u2014 {len(fleet)} node(s), worst first"
    if not fleet:
        return head + "\n\n(no nodes reporting yet)"
    counts: dict[str, int] = {}
    for r in fleet:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    head += " \u00b7 " + " \u00b7 ".join(
        _sgr(f"{counts[s]} {s}", _STATE_SGR.get(s, "0"), color)
        for s in ("dead", "unknown", "critical", "degraded", "stale", "ok") if counts.get(s))
    width = min(22, max(len(r["node"]) for r in fleet))
    lines = [head, "",
             f"  {'node'.ljust(width)} {'state':<9} {'live':<8} {'age':>6} "
             f"{'open':>5} {'disk':>6} {'temp':>6}"]
    for r in fleet:
        hb = r.get("heartbeat") or {}
        disk = f"{hb['disk_used_pct']:.0f}%" if hb.get("disk_used_pct") is not None else "-"
        temp = f"{hb['temp_c']:.0f}C" if hb.get("temp_c") is not None else "-"
        # An open count on a node we cannot currently hear from is not evidence the fault
        # persists, so it is marked rather than printed as a bare fact (hubapi.open_trustworthy).
        n_open = r.get("open_incidents", 0)
        open_s = f"{n_open}" + ("" if r.get("open_trustworthy") or not n_open else "?")
        state = _sgr(r["state"].ljust(9), _STATE_SGR.get(r["state"], "0"), color)
        lines.append(f"{_dot(r['state'], color)} {r['node'][:width].ljust(width)} "
                     f"{state} {r.get('liveness', '?'):<8} {_fmt_age(r.get('age_s')):>6} "
                     f"{open_s:>5} {disk:>6} {temp:>6}")
    return "\n".join(lines)


def density_report(density: dict, *, color: bool = True) -> str:
    """node x hour grid of incident COUNTS (hubapi.incident_density).

    Counts, not measured badness. The old loss heatmap coloured a cell by the loss it had
    samples for, which made "no data this hour" and "fine this hour" identical; a count grid
    cannot lie that way, because an empty cell means nothing was recorded and nothing being
    recorded is exactly what a healthy hour now looks like."""
    counts = density.get("counts", {})
    n = density.get("buckets", 0)
    head = f"INCIDENT DENSITY — {n} hourly bucket(s), incidents per node per hour"
    if not counts:
        return head + "\n\n(no incidents in the window)"
    worst = density.get("worst", {})
    width = min(22, max(len(node) for node in counts))
    lines = [head, ""]
    for node in density.get("nodes") or sorted(counts):
        row = counts.get(node, [])
        wrow = worst.get(node, [])
        cells = []
        for i, c in enumerate(row):
            if not c:
                cells.append("·" if not ASCII else ".")
                continue
            glyph = str(c) if c < 10 else "+"
            rank = wrow[i] if i < len(wrow) else 0
            cells.append(_sgr(glyph, "31" if rank >= 3 else "33", color))
        lines.append(f"{node[:width].ljust(width)} {''.join(cells)}")
    hour0 = density.get("hour0")
    if hour0 is not None and n >= 2:
        start, end = _hhmm(hour0), _hhmm(hour0 + (n - 1) * 3600)
        gap = max(1, n - len(start) - len(end))
        lines.append(" " * (width + 1) + start + " " * gap + end)
    return "\n".join(lines)


# ---------- incident feed + detail ----------

# `unknown` must never render as `ongoing`. The hub reports it when a node went silent while
# an incident was open, so the close transition may simply have had nowhere to go. Giving it
# its own word, colour and glyph is the whole point: presenting a guess as a fact is the
# specific failure this pivot exists to avoid.
_INC_STATE_SGR = {"ongoing": "31", "unknown": "35", "closed": "32"}
_INC_STATE_MARK = {"ongoing": "●", "unknown": "?", "closed": "✓"}
_INC_STATE_MARK_ASCII = {"ongoing": "!", "unknown": "?", "closed": "-"}


def incident_state(inc: dict, *, color: bool = False) -> str:
    """The rendered state word for one incident, with its reason when the hub gave one."""
    st = inc.get("state", "ongoing")
    mark = (_INC_STATE_MARK_ASCII if ASCII else _INC_STATE_MARK).get(st, "?")
    text = f"{mark} {st}"
    if st == "unknown":
        text += f" ({inc.get('unknown_reason') or 'no close received'})"
    return _sgr(text, _INC_STATE_SGR.get(st, "0"), color)


def _inc_duration(inc: dict, until: float | None) -> str:
    if inc.get("duration_s") is not None:
        return analyze._dur(inc["duration_s"])
    if until is not None and inc.get("opened_ts") is not None:
        return analyze._dur(until - inc["opened_ts"]) + "+"
    return "?"


def incidents_feed_report(feed: dict, *, color: bool = True) -> str:
    """The fleet-wide incident feed (hubapi.incidents_feed): what broke, where, when, and
    whether it is still broken."""
    incs = feed.get("incidents", [])
    counts = feed.get("counts", {})
    span_h = (feed.get("until", 0) - feed.get("since", 0)) / 3600
    head = f"INCIDENTS — {len(incs)} in the last {span_h:.1f}h"
    for st in ("ongoing", "unknown", "closed"):
        if counts.get(st):
            head += " · " + _sgr(f"{counts[st]} {st}", _INC_STATE_SGR[st], color)
    if not incs:
        return head + "\n\n(nothing recorded in this window)"
    nw = min(16, max(len(i["node"]) for i in incs))
    lines = [head, "",
             f"{'':<5} {'opened':<12} {'node'.ljust(nw)} {'signal':<26} {'worst':>9} "
             f"{'duration':>9}  state"]
    for i in incs:
        where = f"{i['signal']}/{i['entity']}" if i.get("entity") else i["signal"]
        worst = f"{i['worst_value']:g}" if i.get("worst_value") is not None else "-"
        sev = _sgr(f"{(i.get('severity') or '?'):<5}", _SEV_SGR.get(i.get("severity"), "0"), color)
        lines.append(f"{sev} {_stamp(i['opened_ts']):<12} {i['node'][:nw].ljust(nw)} "
                     f"{where[:26]:<26} {worst:>9} {_inc_duration(i, feed.get('until')):>9}  "
                     f"{incident_state(i, color=color)}")
    if feed.get("truncated"):
        lines.append("")
        lines.append("(truncated at the API limit — narrow with --node or --hours)")
    return "\n".join(lines)


def incident_detail_report(inc: dict, *, color: bool = True) -> str:
    """One incident with the evidence the node captured around it (hubapi.incident_detail).

    The samples ARE the record. There is no continuous series behind this to fall back on, so
    the pre/during/post phases are printed in full rather than summarised into a shape."""
    where = f"{inc['signal']}/{inc['entity']}" if inc.get("entity") else inc["signal"]
    title = f"INCIDENT {inc['uid']} — {inc['node']} — {where}"
    lines = [title, "=" * len(title), ""]
    lines.append(f"state:     {incident_state(inc, color=color)}")
    lines.append(f"severity:  {_sgr(inc.get('severity') or '?', _SEV_SGR.get(inc.get('severity'), '0'), color)}")
    lines.append(f"opened:    {_stamp(inc['opened_ts'])}")
    if inc.get("ended_ts"):
        lines.append(f"ended:     {_stamp(inc['ended_ts'])}")
    if inc.get("duration_s") is not None:
        lines.append(f"duration:  {analyze._dur(inc['duration_s'])}")
    if inc.get("worst_value") is not None:
        lines.append(f"worst:     {inc['worst_value']:g}")
    # Threshold/baseline/z are stored as they were AT EVALUATION TIME so the incident stays
    # readable after a rule change; showing them is the only way a reader can tell whether an
    # old incident would still trip under today's rule.
    for label, key, fmt in (("threshold", "threshold", "g"), ("baseline", "baseline", "g"),
                            ("baseline mad", "baseline_mad", "g"), ("z", "z", ".2f")):
        if inc.get(key) is not None:
            lines.append(f"{label + ':':<10} {inc[key]:{fmt}}")
    if inc.get("rule_hash"):
        lines.append(f"rule:      {inc['rule_hash']}")
    if inc.get("detail"):
        lines.append(f"detail:    {inc['detail']}")

    lines += ["", "transitions:"]
    for t in inc.get("transitions", []):
        lines.append(f"  {_stamp(t['ts'])}  {t['transition']}")

    phases = inc.get("phases") or {}
    samples = inc.get("samples") or []
    lines += ["", f"samples ({len(samples)}):"]
    if not samples:
        lines.append("  (none captured — the node was over its evidence budget, "
                     "or they have not shipped yet)")
    for phase in ("pre", "during", "post"):
        rows = phases.get(phase) or []
        if not rows:
            continue
        vals = [s["value"] for s in rows]
        lines.append(f"  {phase:<7} {len(rows):>3}  {sparkline(vals)}  "
                     f"{_stamp(rows[0]['ts'])} → {_stamp(rows[-1]['ts'])}")
        for s in rows:
            lines.append(f"    {_stamp(s['ts'])}  {s['value']:g}")

    evidence = inc.get("evidence") or []
    if evidence:
        lines += ["", f"log excerpts ({len(evidence)}):"]
        for e in evidence:
            dropped = f", {e['dropped']} bytes dropped" if e.get("dropped") else ""
            lines.append(f"  {_stamp(e['ts'])}  {e.get('source') or '?'} "
                         f"{e.get('path') or ''} ({e.get('reason') or '?'}{dropped})")
            for ln in (e.get("excerpt") or "").splitlines():
                lines.append(f"    | {ln}")
    return "\n".join(lines)
