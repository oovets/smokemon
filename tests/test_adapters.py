"""Adapters degrade gracefully when running on the 'wrong' OS - Linux helpers must
not crash on macOS (and vice versa), and the wifi-stats parser must read the
/proc/net/wireless column layout correctly."""

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


def test_host_helpers_safe_on_wrong_os():
    """All Linux-only helpers should return None / empty when /proc + /sys don't exist
    (i.e. when running on macOS during CI). They must never raise."""
    from smokemon.probes import host
    # These return None or empty on non-Linux without crashing
    assert host._psi_linux() == (None, None, None) or os.path.exists("/proc/pressure/cpu")
    assert isinstance(host._thermal_zones_linux(), dict)
    assert isinstance(host._tcp_metrics_linux(), dict)
    assert isinstance(host._jetson_power_linux(), list)
    assert isinstance(host._sd_wear_linux(), list)
    # pi_throttle_bits returns None when vcgencmd is absent (which it is in CI)
    assert host._pi_throttle_bits() is None or host._vcgencmd is not None


# ---------- macOS parsers ----------

PMSET_NORMAL = (
    "Note: No thermal warning level has been recorded\n"
    "Note: No performance warning level has been recorded\n"
    "Note: No CPU power status has been recorded\n"
)
PMSET_THROTTLED = (
    "CPU_Scheduler_Limit         = 100\n"
    "CPU_Available_CPUs          = 14\n"
    "CPU_Speed_Limit             = 80\n"
)
SWAPUSAGE = "total = 6144.00M  used = 4886.12M  free = 1257.88M  (encrypted)"
IOREG_BATT = (
    '+-o AppleSmartBattery  <class AppleSmartBattery, ...>\n'
    '    | |   "MaxCapacity" = 100\n'
    '    | |   "CurrentCapacity" = 100\n'
    '    | |   "Amperage" = -1234\n'
    '    | |   "InstantAmperage" = -1500\n'
    '    | |   "Voltage" = 12500\n'
    '    | |   "IsCharging" = No\n'
    '    | |   "ExternalConnected" = No\n'
)
NETSTAT_TCP = (
    "tcp:\n"
    "\t100 packet sent\n"
    "\t\t50 data packet (0 byte) retransmitted\n"
    "\t\t2 bad reset\n"
    "\t5 retransmit timeout\n"
    "\t\t3 connection dropped by rexmit timeout\n"
)
NETSTAT_UDP = (
    "udp:\n"
    "\t\t10 with bad checksum\n"
    "\t\t7 dropped due to no socket\n"
)


def _patch_sh(handlers):
    """Patch host._sh so each fake command returns its registered string output.
    handlers is a dict mapping the first argv arg (or full tuple) to stdout text."""
    from smokemon.probes import host

    def fake(args, timeout=5.0):
        key = tuple(args)
        if key in handlers:
            return handlers[key]
        # fallback: match on the first two args (cmd + first flag) for ergonomic mocks
        return handlers.get(tuple(args[:2]), handlers.get(args[0]))

    return patch.object(host, "_sh", fake)


def test_macos_thermal_normal():
    from smokemon.probes import host
    with _patch_sh({("pmset", "-g"): PMSET_NORMAL}):
        zones = host._thermal_macos()
    assert zones == {"cpu_speed_limit_pct": 100.0}


def test_macos_thermal_throttled():
    from smokemon.probes import host
    with _patch_sh({("pmset", "-g"): PMSET_THROTTLED}):
        zones = host._thermal_macos()
    assert zones == {"cpu_speed_limit_pct": 80.0}


def test_macos_swap():
    from smokemon.probes import host
    with _patch_sh({("sysctl", "-n"): SWAPUSAGE}):
        pct = host._swap_macos()
    # 4886.12 / 6144 = 0.7953... -> 79.5%
    assert pct == 79.5


def test_macos_power_battery_discharging():
    from smokemon.probes import host
    with _patch_sh({("ioreg", "-rc"): IOREG_BATT}):
        rails = host._power_macos()
    assert len(rails) == 1
    r = rails[0]
    assert r["rail"] == "battery"
    # 12.5 V * 1.5 A = 18.75 W (uses InstantAmperage, takes abs of negative)
    assert r["volts"] == 12.5
    assert r["amps"] == 1.5
    assert r["watts"] == 18.75


def test_macos_power_no_battery():
    """Desktop / mac mini - no AppleSmartBattery in ioreg output."""
    from smokemon.probes import host
    with _patch_sh({("ioreg", "-rc"): "no battery here"}):
        assert host._power_macos() == []


def test_macos_tcp_parses_netstat():
    from smokemon.probes import host
    handlers = {
        ("netstat", "-s", "-p", "tcp"): NETSTAT_TCP,
        ("netstat", "-s", "-p", "udp"): NETSTAT_UDP,
    }

    def fake(args, timeout=5.0):
        return handlers.get(tuple(args))

    with patch.object(host, "_sh", fake):
        m = host._tcp_metrics_macos()
    assert m["retrans_segs"] == 50
    assert m["out_rsts"] == 2
    assert m["estab_resets"] == 3
    assert m["udp_in_errors"] == 10
    assert m["udp_no_ports"] == 7
    assert m["conntrack_used"] is None  # pf-state count requires root
    assert m["conntrack_max"] is None
