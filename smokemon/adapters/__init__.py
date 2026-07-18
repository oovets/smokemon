"""OS-specific data collection behind a common interface.

Linux only. This package used to dispatch on platform.system() across a linux/darwin pair;
the darwin adapter was removed and the indirection is kept only so probes have one import
site for host-shaped reads that may later need per-distro or per-board variants.

Exports:
  read_net_counters()      -> [(iface, ibytes, obytes, ipkts, opkts)]  (raw, no relabel)
  detect_tailscale_iface() -> str | None
  wifi_probe()             -> dict | None
"""

from .linux import detect_tailscale_iface, read_net_counters, read_net_errors, wifi_probe

__all__ = ["read_net_counters", "read_net_errors", "detect_tailscale_iface", "wifi_probe"]
