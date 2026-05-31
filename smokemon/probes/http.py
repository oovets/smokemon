"""HTTP/S timing via curl HEAD: DNS / TCP connect / TLS / TTFB / total, in ms."""

import subprocess
import time

from .. import config, core, events, schema

_FMT = ("code=%{http_code} dns=%{time_namelookup} conn=%{time_connect} "
        "tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}")
_CURL = config.cli_path("SMOKEMON_CURL", "curl")


def _probe(url: str) -> dict | None:
    try:
        proc = subprocess.run([_CURL, "-sI", "-o", "/dev/null", "--max-time", "10", "-w", _FMT, url],
                              capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        core.log(f"curl error {url}: {e!r}")
        return None
    v = dict(tok.split("=", 1) for tok in proc.stdout.split() if "=" in tok)
    if "total" not in v:
        return None
    return {"url": url, "http_code": int(v.get("code", 0)),
            "dns_ms": float(v["dns"]) * 1000, "connect_ms": float(v["conn"]) * 1000,
            "tls_ms": float(v["tls"]) * 1000, "ttfb_ms": float(v["ttfb"]) * 1000, "total_ms": float(v["total"]) * 1000}


def collect(conn) -> None:
    ts = time.time()
    rows = [{"ts": ts, **r} for url in config.HTTP_URLS if (r := _probe(url))]
    if rows:
        schema.insert(conn, "http_samples", rows)
        conn.commit()
    # Edge event per URL off the request we already made: a 5xx or no-response trips a warn once;
    # it clears (quiet) when the URL answers < 500 again. No extra request -> no footprint.
    for r in rows:
        code = r["http_code"]
        events.edge(conn, code == 0 or code >= 500, f"http:{r['url']}", source="http",
                    severity="warn", event="http-error",
                    detail=f"{r['url']} -> {code or 'no response'}", clear_detail=f"{r['url']} {code}")
