"""OS-specific data collection behind a common interface. Dispatch by platform.

Exports:
  read_net_counters()      -> [(iface, ibytes, obytes, ipkts, opkts)]  (raw, no relabel)
  detect_tailscale_iface() -> str | None
  wifi_probe()             -> dict | None
"""

import platform

SYSTEM = platform.system()  # "Darwin" | "Linux"

if SYSTEM == "Linux":
    from .linux import detect_tailscale_iface, read_net_counters, wifi_probe
else:  # Darwin and fallback
    from .darwin import detect_tailscale_iface, read_net_counters, wifi_probe

__all__ = ["SYSTEM", "read_net_counters", "detect_tailscale_iface", "wifi_probe"]
