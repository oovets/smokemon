"""Host helpers degrade gracefully on Linux boxes that lack the optional /sys and /proc
interfaces they read (no Jetson rails, no vcgencmd, no PSI on older kernels), and the
wifi-stats parser must read the /proc/net/wireless column layout correctly."""

import io
import os
from unittest.mock import patch


def test_linux_wireless_stats_parses_columns():
    """Verify column indexing matches the documented /proc/net/wireless layout:
       face: status link level noise   nwid crypt frag retry misc   beacon"""
    from smokemon.adapters import linux

    raw = (
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
        " wlan0: 0000   58.  -52.  -256        0      0      0    142      3       17\n"
    )

    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/net/wireless":
            return io.StringIO(raw)
        return real_open(path, *a, **kw)

    with patch("builtins.open", fake_open):
        stats = linux._wireless_stats("wlan0")
    assert stats["retry_count"] == 142
    assert stats["beacon_loss"] == 17
    assert stats["discard_count"] == 3  # nwid(0) + crypt(0) + frag(0) + misc(3)
    assert stats["noise_dbm"] is None  # -256 is the sentinel for "no measurement"


def test_linux_wireless_stats_missing_iface():
    """Asking for an iface not present in /proc/net/wireless returns all-None."""
    from smokemon.adapters import linux

    raw = (
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
    )
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/net/wireless":
            return io.StringIO(raw)
        return real_open(path, *a, **kw)

    with patch("builtins.open", fake_open):
        stats = linux._wireless_stats("missing0")
    assert all(v is None for v in stats.values())


def test_host_helpers_safe_when_interfaces_absent():
    """Every optional /sys + /proc reader returns None / empty rather than raising when the
    interface is missing -- a generic x86 box has no Jetson INA3221 rails and no vcgencmd,
    and PSI is absent on kernels older than 4.20."""
    from smokemon.probes import host
    # Absent interface -> None / empty, never an exception
    assert host._psi_linux() == (None, None, None) or os.path.exists("/proc/pressure/cpu")
    assert isinstance(host._thermal_zones_linux(), dict)
    assert isinstance(host._tcp_metrics_linux(), dict)
    assert isinstance(host._jetson_power_linux(), list)
    assert isinstance(host._sd_wear_linux(), list)
    # pi_throttle_bits returns None when vcgencmd is absent (non-Pi hardware)
    assert host._pi_throttle_bits() is None or host._vcgencmd is not None
