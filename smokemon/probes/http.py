"""HTTP/S timing via curl HEAD: DNS / TCP connect / TLS / TTFB / total, in ms."""

import subprocess
import time

from .. import config, core, schema

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
