"""Synthetic transactions (roadmap X6): scripted multi-step checks that go beyond the
single-shot ping/http probes - captive-portal / interception detection and a
DNS-over-HTTPS resolution check. Records pass/fail + latency to synthetic_samples.

Node-side, stdlib (urllib). Opt-in via SMOKEMON_SYNTHETIC=1 so the monitor makes no
extra external requests unless asked. The classification helpers are pure (no socket)
so they unit-test without the network."""

import json
import sys
import time
import urllib.error
import urllib.request

from .. import config, core, schema


def classify_captive(status: int | None, body: str) -> tuple[bool, str]:
    """A 204-with-empty-body endpoint is the standard connectivity check. (ok, detail):
    ok when we got exactly that; otherwise the response shape tells us what intercepted
    it (a portal login page is a 200 with a body; a redirect is 3xx)."""
    if status == 204 and not body.strip():
        return True, "204 no content (clean)"
    if status is None:
        return False, "no response (network down?)"
    if 300 <= status < 400:
        return False, f"redirect {status} (captive portal?)"
    if status == 200 and body.strip():
        return False, f"200 with {len(body)} byte body (captive portal / interception)"
    return False, f"unexpected status {status}"


def doh_has_answer(json_text: str) -> tuple[bool, str]:
    """RFC 8484 JSON DoH: Status 0 (NOERROR) with a non-empty Answer means the resolver
    works. (ok, detail)."""
    try:
        data = json.loads(json_text)
    except (ValueError, TypeError):
        return False, "invalid DoH JSON"
    if data.get("Status") != 0:
        return False, f"DoH rcode {data.get('Status')}"
    answers = data.get("Answer") or []
    if not answers:
        return False, "DoH NOERROR but no answer"
    ips = [a.get("data") for a in answers if a.get("type") in (1, 28)]
    return True, "resolved " + (", ".join(filter(None, ips)) or "ok")


def _timed_get(url: str, headers: dict | None = None, timeout: float = 10.0):
    """(status, body, latency_ms) for a GET; (None, '', latency) on failure."""
    req = urllib.request.Request(url, headers=headers or {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(65536).decode(errors="replace")
            return r.status, body, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as e:
        return e.code, "", (time.perf_counter() - t0) * 1000.0
    except Exception:  # noqa: BLE001
        return None, "", (time.perf_counter() - t0) * 1000.0


def _row(probe: str, ok: bool, latency: float, detail: str) -> dict:
    return {"ts": time.time(), "probe": probe, "ok": 1 if ok else 0,
            "latency_ms": round(latency, 1), "detail": detail}


def collect(conn) -> None:
    if not config.SYNTHETIC_ENABLED:
        return
    rows = []

    status, body, ms = _timed_get(config.SYNTHETIC_CAPTIVE_URL)
    ok, detail = classify_captive(status, body)
    rows.append(_row("captive-portal", ok, ms, detail))

    doh_url = f"{config.SYNTHETIC_DOH_URL}?name={config.SYNTHETIC_DOH_NAME}&type=A"
    status, body, ms = _timed_get(doh_url, headers={"Accept": "application/dns-json"})
    if status == 200:
        ok, detail = doh_has_answer(body)
    else:
        ok, detail = False, f"DoH HTTP {status}"
    rows.append(_row("doh", ok, ms, detail))

    schema.insert(conn, "synthetic_samples", rows)
    conn.commit()
    bad = [r["probe"] for r in rows if not r["ok"]]
    core.log(f"synthetic: {len(rows)} check(s)" + (f", FAIL: {', '.join(bad)}" if bad else " ok"))


def main() -> int:
    conn = core.connect(config.DB_PATH)
    schema.init_node(conn)
    if not config.SYNTHETIC_ENABLED:
        core.log("synthetic: SMOKEMON_SYNTHETIC not set, skipping")
    collect(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
