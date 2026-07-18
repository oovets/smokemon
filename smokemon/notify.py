"""Push/webhook alerting (roadmap S4): fire an alert from stored incidents to ntfy,
a Slack or Discord incoming webhook, or any generic JSON endpoint. Pure stdlib
(urllib), hub- or node-side. The destination kind is auto-detected from the URL host
unless SMOKEMON_NOTIFY_KIND pins it.

Payload construction is split from the network send so it can be unit-tested without
a socket, and incidents are gated by severity so an all-clear window never alerts."""

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime

from . import analyze, config, core, query


def detect_kind(url: str) -> str:
    """'ntfy' | 'slack' | 'discord' | 'generic' from the URL host/path."""
    h = url.lower()
    if "ntfy" in h:
        return "ntfy"
    if "hooks.slack.com" in h:
        return "slack"
    if "discord" in h:
        return "discord"
    return "generic"


def build_request(url: str, title: str, body: str, kind: str | None = None) -> urllib.request.Request:
    """Per-kind POST request. ntfy takes a plain-text body with the title in a header;
    Slack/Discord want their chat JSON shape; generic gets {title, body, source}."""
    kind = kind or config.NOTIFY_KIND or detect_kind(url)
    if kind == "ntfy":
        data = body.encode()
        headers = {"Title": title, "Content-Type": "text/plain; charset=utf-8"}
    elif kind == "slack":
        data = json.dumps({"text": f"*{title}*\n{body}"}).encode()
        headers = {"Content-Type": "application/json"}
    elif kind == "discord":
        data = json.dumps({"content": f"**{title}**\n{body}"}).encode()
        headers = {"Content-Type": "application/json"}
    else:
        data = json.dumps({"title": title, "body": body, "source": "smokemon"}).encode()
        headers = {"Content-Type": "application/json"}
    return urllib.request.Request(url, data=data, method="POST", headers=headers)


def send(title: str, body: str, url: str | None = None, kind: str | None = None,
         timeout: float = 15) -> bool:
    """POST one alert. Returns True on a 2xx. No-op (False) when no URL is configured."""
    url = url or config.NOTIFY_URL
    if not url:
        core.log("notify: SMOKEMON_NOTIFY_URL not set, skipping")
        return False
    try:
        with urllib.request.urlopen(build_request(url, title, body, kind), timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError) as e:
        core.log(f"notify failed: {e!r}")
        return False


def summarize_incidents(incidents: list[dict], node: str | None = None,
                        min_severity: int | None = None) -> tuple[str | None, str | None]:
    """(title, body) for incidents at/above min_severity, or (None, None) when none
    qualify - the caller then sends nothing. Incidents are the reconstructed dicts from
    query.load_incidents, whose severity is a word; the configured bar is numeric, so it is
    compared through analyze.severity_rank."""
    min_sev = config.NOTIFY_MIN_SEVERITY if min_severity is None else min_severity
    q = [i for i in incidents if analyze.severity_rank(i.get("severity")) >= min_sev]
    if not q:
        return None, None
    name = node or config.NODE
    worst = max(q, key=lambda i: analyze.severity_rank(i.get("severity")))
    label = f"{worst['signal']} {worst['entity']}" if worst.get("entity") else worst["signal"]
    title = f"smokemon {name}: {label} (+{len(q) - 1} more)" if len(q) > 1 \
        else f"smokemon {name}: {label}"
    lines = []
    for i in sorted(q, key=lambda i: (analyze.severity_rank(i.get("severity")),
                                      i["duration_s"] or 0.0), reverse=True)[:20]:
        hhmm = datetime.fromtimestamp(i["opened_ts"]).strftime("%H:%M")
        sig = f"{i['signal']} {i['entity']}" if i.get("entity") else i["signal"]
        state = "ongoing" if i["state"] == "ongoing" else f"{i['duration_s'] or 0:.0f}s"
        lines.append(f"[{hhmm}] {i['severity']} {sig}: {state}")
    return title, "\n".join(lines)


def alert_from_db(conn, since, until, node=None, min_severity=None, url=None) -> int:
    """Read the incidents in [since, until] and fire one summary alert if any clear the
    severity bar. Returns the number of incidents alerted on (0 = nothing sent)."""
    incidents = query.load_incidents(conn, since, until, node)
    title, body = summarize_incidents(incidents, node, min_severity)
    if not title:
        return 0
    sent = send(title, body, url)
    n = body.count("\n") + 1 if sent else 0
    if sent:
        core.log(f"notify: alerted on {n} incident(s)")
    return n


def main() -> int:
    """Standalone: scan the last window of the DB and alert (for a timer/cron). Window
    via SMOKEMON args is not parsed here; defaults to the last hour of the node DB."""
    import time
    if not config.NOTIFY_URL:
        core.log("notify: SMOKEMON_NOTIFY_URL not set, nothing to do")
        return 0
    conn = query.open_ro(config.DB_PATH)
    until = time.time()
    n = alert_from_db(conn, until - 3600, until)
    conn.close()
    core.log(f"notify: {n} incident(s) in the last hour")
    return 0


if __name__ == "__main__":
    sys.exit(main())
