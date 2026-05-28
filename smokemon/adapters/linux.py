"""Linux adapters: /proc/net/dev counters, tailscale0, iw/proc WiFi. Stdlib + iw."""

import ipaddress
import os
import re
import shutil
import subprocess

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_SKIP = ("lo", "veth", "docker", "br-", "virbr", "vnet", "tap")


def detect_tailscale_iface() -> str | None:
    try:
        with open("/proc/net/dev") as f:
            if any(line.split(":", 1)[0].strip() == "tailscale0" for line in f if ":" in line):
                return "tailscale0"
    except OSError:
        pass
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 4 and p[2] == "inet":
            try:
                if ipaddress.ip_address(p[3].split("/")[0]) in _TAILSCALE_NET:
                    return p[1]
            except ValueError:
                continue
    return None


def read_net_counters() -> list[tuple[str, int, int, int, int]]:
    rows: list[tuple[str, int, int, int, int]] = []
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
    except OSError:
        return rows
    for line in lines[2:]:  # two header rows
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        iface = name.strip()
        if iface.startswith(_SKIP):
            continue
        f = rest.split()
        # rx: bytes packets ... (0,1); tx: bytes packets ... (8,9)
        if len(f) < 16:
            continue
        try:
            rows.append((iface, int(f[0]), int(f[8]), int(f[1]), int(f[9])))  # iface, ib, ob, ip, op
        except (ValueError, IndexError):
            continue
    return rows


def _wifi_iface() -> str | None:
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.isdir(f"/sys/class/net/{name}/wireless"):
                return name
    except OSError:
        return None
    return None


def _wireless_stats(iface: str) -> dict:
    """Parse /proc/net/wireless data row for `iface`. Returns noise + counters when
    present; missing values become None. The data line for an interface has the layout:
       face: status link level noise   nwid crypt frag retry misc   beacon
    indices after the colon: 0=status 1=link 2=level 3=noise 4..8=discard 9=beacon."""
    out: dict = {"noise_dbm": None, "retry_count": None, "discard_count": None, "beacon_loss": None}
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if not line.strip().startswith(iface + ":"):
                    continue
                cols = line.split(":", 1)[1].split()
                try:
                    noise = int(float(cols[3].rstrip(".")))
                    out["noise_dbm"] = noise if noise > -256 else None
                except (IndexError, ValueError):
                    pass
                try:
                    out["retry_count"] = int(cols[7].rstrip("."))
                except (IndexError, ValueError):
                    pass
                try:
                    discard = sum(int(cols[i].rstrip(".")) for i in (4, 5, 6, 8))
                    out["discard_count"] = discard
                except (IndexError, ValueError):
                    pass
                try:
                    out["beacon_loss"] = int(cols[9].rstrip("."))
                except (IndexError, ValueError):
                    pass
                break
    except OSError:
        pass
    return out


def wifi_probe() -> dict | None:
    iface = _wifi_iface()
    iw = shutil.which("iw")
    if not iface or not iw:
        return None
    try:
        out = subprocess.run([iw, "dev", iface, "link"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    if not out.strip() or "Not connected" in out:
        return None

    def s(pat: str) -> str | None:
        m = re.search(pat, out)
        return m.group(1).strip() if m else None

    signal, txrate = s(r"signal:\s*(-?\d+)"), s(r"tx bitrate:\s*([\d.]+)")
    bssid = s(r"Connected to ([0-9a-fA-F:]{17})")
    extra = _wireless_stats(iface)
    return {
        "ssid": s(r"SSID:\s*(.+)"),
        "channel": s(r"freq:\s*(\d+)"),
        "phy_mode": None,
        "rssi_dbm": int(signal) if signal else None,
        "noise_dbm": extra["noise_dbm"],
        "tx_rate_mbps": float(txrate) if txrate else None,
        "bssid": bssid.lower() if bssid else None,
        "retry_count": extra["retry_count"],
        "discard_count": extra["discard_count"],
        "beacon_loss": extra["beacon_loss"],
    }
