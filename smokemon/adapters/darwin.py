"""macOS adapters: netstat byte counters, Tailscale iface, system_profiler WiFi."""

import ipaddress
import re
import subprocess

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale CGNAT range
_SKIP = ("lo", "gif", "stf", "anpi", "bridge", "ap")
_LINK_RE = re.compile(r"<Link#\d+>")
_INET_RE = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)")


def detect_tailscale_iface() -> str | None:
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    cur = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            cur = line.split(":", 1)[0]
        else:
            m = _INET_RE.search(line)
            if m and ipaddress.ip_address(m.group(1)) in _TAILSCALE_NET:
                return cur
    return None


def read_net_counters() -> list[tuple[str, int, int, int, int]]:
    proc = subprocess.run(["/usr/sbin/netstat", "-ibn"], capture_output=True, text=True, timeout=15)
    rows: list[tuple[str, int, int, int, int]] = []
    for line in proc.stdout.splitlines():
        if not _LINK_RE.search(line):
            continue  # only the <Link#N> row holds totals; avoids double counting
        f = line.split()
        # Address column is absent for utun et al., so index the constant last 7 fields.
        if len(f) < 10 or f[0].startswith(_SKIP):
            continue
        try:
            rows.append((f[0], int(f[-5]), int(f[-2]), int(f[-7]), int(f[-4])))  # iface, ib, ob, ip, op
        except (ValueError, IndexError):
            continue
    return rows


_SIGNOISE_RE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")
_TXRATE_RE = re.compile(r"Transmit Rate:\s*([\d.]+)")
_CHANNEL_RE = re.compile(r"Channel:\s*(.+)")
_PHY_RE = re.compile(r"PHY Mode:\s*(.+)")


def wifi_probe() -> dict | None:
    try:
        out = subprocess.run(["/usr/sbin/system_profiler", "SPAirPortDataType"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return None
    lines = out.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if "Current Network Information:" in l)
    except StopIteration:
        return None
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    block, ssid = [], None
    for l in lines[start + 1:]:
        if not l.strip():
            continue
        if len(l) - len(l.lstrip()) <= base_indent:
            break  # dedent back to section level = end of current network block
        if ssid is None and l.rstrip().endswith(":") and ":" not in l.strip()[:-1]:
            ssid = l.strip().rstrip(":")
        block.append(l)
    text = "\n".join(block)
    m = _SIGNOISE_RE.search(text)
    if not m:
        return None
    tx, ch, phy = _TXRATE_RE.search(text), _CHANNEL_RE.search(text), _PHY_RE.search(text)
    return {
        "ssid": ssid,
        "channel": ch.group(1).strip() if ch else None,
        "phy_mode": phy.group(1).strip() if phy else None,
        "rssi_dbm": int(m.group(1)),
        "noise_dbm": int(m.group(2)),
        "tx_rate_mbps": float(tx.group(1)) if tx else None,
    }
