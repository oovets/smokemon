"""Per-interface link errors, fed to the detector as a rate.

Samples /proc/net/dev, canonicalises the interface name (the Tailscale interface is relabeled
"tailscale" so an incident keeps the same entity across a tailscaled restart that renumbers
it), and hands the value to incidents.evaluate(). Nothing is written per cycle: the detector
keeps samples in memory and persists only what a rule confirms.

Throughput is not sampled at all -- see _feed_error_rates for why traffic volume is context
rather than signal."""

import time

from .. import adapters, incidents

_ts = {"iface": None, "exp": 0.0}
_prev_err: dict[str, tuple[int, float]] = {}   # iface -> (cumulative errs+drops, ts)


def _tailscale_iface() -> str | None:
    now = time.time()
    if now >= _ts["exp"]:
        _ts["iface"] = adapters.detect_tailscale_iface()
        _ts["exp"] = now + 300
    return _ts["iface"]


def collect(conn) -> None:
    _feed_error_rates(conn, time.time(), _tailscale_iface())


def _feed_error_rates(conn, ts: float, tsi: str | None) -> None:
    """Interface errors/drops as a per-second rate.

    Throughput is deliberately NOT a signal: high traffic is context, not an anomaly, and a
    z-rule over it would open an incident every time somebody downloaded something large.
    Errors are different -- a link dropping frames is wrong at any bandwidth.

    A counter that goes backwards means the interface (or the box) restarted; that is a reset,
    not a negative error rate, so it re-seeds without emitting."""
    for iface, errs in adapters.read_net_errors():
        name = "tailscale" if iface == tsi else iface
        prev = _prev_err.get(name)
        _prev_err[name] = (errs, ts)
        if prev is None:
            continue
        prev_errs, prev_ts = prev
        dt = ts - prev_ts
        if dt <= 0 or errs < prev_errs:
            continue
        incidents.evaluate(conn, "net.err_rate", name, (errs - prev_errs) / dt, ts)
