"""Opt-in external HTTP scrapes with bounded footprint.

Reads explicit health/metrics endpoints only. No log tailing, no Docker/journal scans,
and no persistent subprocesses: every scrape has a short timeout, max body bytes, and a
small metric cap. Config format:

  SMOKEMON_EXT_HTTP='app=http://127.0.0.1:8080/health;node=http://127.0.0.1:9100/metrics|metrics=node_load1'

Each endpoint always emits source.up and source.latency_ms. JSON responses contribute
numeric/bool fields. OpenMetrics/Prometheus text responses contribute numeric samples
from the explicit per-endpoint allowlist; without an allowlist they only emit up/latency.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import config, core, schema


@dataclass
class Endpoint:
    source: str
    url: str
    kind: str | None = None
    metrics: set[str] | None = None


def _parse_spec(spec: str) -> Endpoint | None:
    head, *opts = spec.split("|")
    if "=" not in head:
        return None
    source, url = head.split("=", 1)
    source, url = source.strip(), url.strip()
    if not source or not url:
        return None
    kind = None
    metrics = None
    for opt in opts:
        if "=" not in opt:
            continue
        k, v = (p.strip() for p in opt.split("=", 1))
        if k == "kind" and v in ("json", "metrics"):
            kind = v
        elif k == "metrics":
            metrics = {m.strip() for m in v.split(",") if m.strip()}
    return Endpoint(source=source, url=url, kind=kind, metrics=metrics)


def _endpoints() -> list[Endpoint]:
    out = []
    for spec in config.EXT_HTTP:
        ep = _parse_spec(spec)
        if ep:
            out.append(ep)
        else:
            core.log(f"ext: ignoring invalid SMOKEMON_EXT_HTTP spec {spec!r}")
    return out


def _fetch(url: str) -> tuple[int, bytes, str, float]:
    req = urllib.request.Request(url, headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.1"})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=config.EXT_TIMEOUT) as resp:
        body = resp.read(config.EXT_MAX_BYTES + 1)
        if len(body) > config.EXT_MAX_BYTES:
            raise ValueError("response too large")
        elapsed_ms = (time.monotonic() - start) * 1000
        content_type = resp.headers.get("Content-Type", "")
        return resp.status, body, content_type, elapsed_ms


def _flat_json(obj, prefix: str = "", depth: int = 0):
    if depth > 5:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = re.sub(r"[^A-Za-z0-9_]+", "_", str(k)).strip("_")
            name = f"{prefix}_{key}" if prefix else key
            yield from _flat_json(v, name, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            yield from _flat_json(v, f"{prefix}_{i}", depth + 1)
    elif isinstance(obj, bool):
        yield prefix, 1.0 if obj else 0.0
    elif isinstance(obj, (int, float)) and obj == obj:
        yield prefix, float(obj)


def _json_metrics(body: bytes, allow: set[str] | None) -> list[tuple[str, float, str | None, str | None]]:
    data = json.loads(body.decode("utf-8", "replace"))
    out = []
    for name, value in _flat_json(data):
        if not name:
            continue
        if allow and name not in allow:
            continue
        out.append((name, value, None, None))
        if len(out) >= config.EXT_MAX_METRICS:
            break
    return out


def _metric_name(line: str) -> str:
    return line.split("{", 1)[0].split(None, 1)[0]


def _openmetrics(body: bytes, allow: set[str] | None) -> list[tuple[str, float, str | None, str | None]]:
    if not allow:
        return []
    out = []
    for raw in body.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = _metric_name(line)
        if name not in allow:
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        try:
            value = float(parts[1])
        except ValueError:
            continue
        labels = line[len(name):].split(None, 1)[0] if "{" in line[:line.find(" ") if " " in line else len(line)] else None
        out.append((name, value, None, labels))
        if len(out) >= config.EXT_MAX_METRICS:
            break
    return out


def _kind(ep: Endpoint, body: bytes, content_type: str) -> str:
    if ep.kind:
        return ep.kind
    ctype = content_type.lower()
    stripped = body.lstrip()
    if "json" in ctype or stripped.startswith((b"{", b"[")):
        return "json"
    return "metrics"


def _rows_for(ep: Endpoint, ts: float):
    metrics = []
    events = []
    try:
        status, body, content_type, elapsed_ms = _fetch(ep.url)
        ok = 1.0 if 200 <= status < 400 else 0.0
        metrics.append({"ts": ts, "source": ep.source, "metric": "up", "value": ok, "unit": "", "labels": ""})
        metrics.append({"ts": ts, "source": ep.source, "metric": "latency_ms",
                        "value": elapsed_ms, "unit": "ms", "labels": ""})
        if ok:
            kind = _kind(ep, body, content_type)
            parsed = _json_metrics(body, ep.metrics) if kind == "json" else _openmetrics(body, ep.metrics)
            for name, value, unit, labels in parsed:
                metrics.append({"ts": ts, "source": ep.source, "metric": name,
                                "value": value, "unit": unit or "", "labels": labels or ""})
        else:
            events.append({"ts": ts, "source": ep.source, "severity": "warn",
                           "event": "http-status", "detail": str(status)})
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        metrics.append({"ts": ts, "source": ep.source, "metric": "up", "value": 0.0, "unit": "", "labels": ""})
        events.append({"ts": ts, "source": ep.source, "severity": "warn",
                       "event": "scrape-failed", "detail": e.__class__.__name__})
    return metrics, events


def collect(conn) -> None:
    eps = _endpoints()
    if not eps:
        return
    ts = time.time()
    metrics = []
    events = []
    for ep in eps:
        m, e = _rows_for(ep, ts)
        metrics.extend(m)
        events.extend(e)
    if metrics:
        schema.insert(conn, "ext_metrics", metrics)
    if events:
        schema.insert(conn, "ext_events", events)
    if metrics or events:
        conn.commit()
