"""Per-interface bandwidth: cumulative byte counters (delta -> Mbit/s done at plot time).
The Tailscale interface is relabeled "tailscale"; its detection is cached for 5 min."""

import time

from .. import adapters, schema

_ts = {"iface": None, "exp": 0.0}


def _tailscale_iface() -> str | None:
    now = time.time()
    if now >= _ts["exp"]:
        _ts["iface"] = adapters.detect_tailscale_iface()
        _ts["exp"] = now + 300
    return _ts["iface"]


def collect(conn) -> None:
    ts = time.time()
    tsi = _tailscale_iface()
    rows = [{"ts": ts, "iface": "tailscale" if iface == tsi else iface,
             "ibytes": ib, "obytes": ob, "ipkts": ip, "opkts": op}
            for (iface, ib, ob, ip, op) in adapters.read_net_counters()]
    schema.insert(conn, "net_samples", rows)
    conn.commit()
