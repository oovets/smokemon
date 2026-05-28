#!/usr/bin/env python3
"""smokemon platform adapters — OS-beroende insamling bakom ett gemensamt gränssnitt.
Importeras av collectors så de förblir OS-agnostiska. Ren stdlib.

Exporterar:
  read_net_counters()      -> [(iface, ibytes, obytes, ipkts, opkts)]
  detect_tailscale_iface() -> str | None
  wifi_probe()             -> dict | None
  NODE                     -> nodens namn (SMOKEMON_NODE el. hostname)
  cli_path(env, name)      -> sökväg till CLI (env -> which -> name)
  ensure_node_column(conn, tables) -> migrera in 'node'-kolumn i befintliga tabeller
"""

import ipaddress
import os
import platform
import re
import shutil
import socket
import subprocess

_SYS = platform.system()  # "Darwin" | "Linux"

NODE = os.environ.get("SMOKEMON_NODE") or socket.gethostname()

# Tailscale CGNAT-range; iface med adress här märks "tailscale" (stabilt label).
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# Virtuella/loopback-interfaces att hoppa över (per OS).
_SKIP_DARWIN = ("lo", "gif", "stf", "anpi", "bridge", "ap")
_SKIP_LINUX = ("lo", "veth", "docker", "br-", "virbr", "vnet", "tap")


def cli_path(env_var: str, name: str) -> str:
    """Sökväg till en CLI: explicit env-var -> PATH-uppslag -> råa namnet (PATH vid exec)."""
    return os.environ.get(env_var) or shutil.which(name) or name


def ensure_node_column(conn, tables) -> None:
    """Lägg till en 'node'-kolumn i befintliga tabeller som saknar den och fyll i NODE.
    No-op för färska tabeller (de skapas redan med kolumnen av init_db)."""
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if cols and "node" not in cols:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN node TEXT")
            conn.execute(f"UPDATE {t} SET node = ? WHERE node IS NULL", (NODE,))
    conn.commit()


# ---- net counters -----------------------------------------------------------

_LINK_RE = re.compile(r"<Link#\d+>")
_INET_RE = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)")


def _detect_tailscale_iface_darwin() -> str | None:
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


def _detect_tailscale_iface_linux() -> str | None:
    # tailscale0 är det stabila namnet på Linux.
    try:
        with open("/proc/net/dev") as f:
            names = [line.split(":", 1)[0].strip() for line in f if ":" in line]
        if "tailscale0" in names:
            return "tailscale0"
    except OSError:
        pass
    # Fallback: scanna efter ett iface med en 100.64.0.0/10-adress.
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            try:
                if ipaddress.ip_address(parts[3].split("/")[0]) in _TAILSCALE_NET:
                    return parts[1]
            except ValueError:
                continue
    return None


def _read_net_counters_darwin() -> list[tuple[str, int, int, int, int]]:
    ts_iface = _detect_tailscale_iface_darwin()
    proc = subprocess.run(["/usr/sbin/netstat", "-ibn"], capture_output=True, text=True, timeout=15)
    rows: list[tuple[str, int, int, int, int]] = []
    for line in proc.stdout.splitlines():
        if not _LINK_RE.search(line):
            continue  # bara <Link#N>-raden har totalsumman, undvik dubbelräkning
        f = line.split()
        if len(f) < 10:
            continue
        iface = f[0]
        if iface.startswith(_SKIP_DARWIN):
            continue
        if ts_iface and iface == ts_iface:
            iface = "tailscale"
        try:
            ipkts, ibytes = int(f[-7]), int(f[-5])
            opkts, obytes = int(f[-4]), int(f[-2])
        except (ValueError, IndexError):
            continue
        rows.append((iface, ibytes, obytes, ipkts, opkts))
    return rows


def _read_net_counters_linux() -> list[tuple[str, int, int, int, int]]:
    ts_iface = _detect_tailscale_iface_linux()
    rows: list[tuple[str, int, int, int, int]] = []
    try:
        with open("/proc/net/dev") as fh:
            lines = fh.readlines()
    except OSError:
        return rows
    for line in lines[2:]:  # två header-rader
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        iface = name.strip()
        if iface.startswith(_SKIP_LINUX):
            continue
        f = rest.split()
        # receive: bytes packets errs drop fifo frame compressed multicast (0-7)
        # transmit: bytes packets errs drop fifo colls carrier compressed (8-15)
        if len(f) < 16:
            continue
        try:
            ibytes, ipkts = int(f[0]), int(f[1])
            obytes, opkts = int(f[8]), int(f[9])
        except (ValueError, IndexError):
            continue
        if ts_iface and iface == ts_iface:
            iface = "tailscale"
        rows.append((iface, ibytes, obytes, ipkts, opkts))
    return rows


# ---- WiFi -------------------------------------------------------------------

# macOS (system_profiler)
_SIGNOISE_RE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")
_TXRATE_RE = re.compile(r"Transmit Rate:\s*([\d.]+)")
_CHANNEL_RE = re.compile(r"Channel:\s*(.+)")
_PHY_RE = re.compile(r"PHY Mode:\s*(.+)")


def _wifi_probe_darwin() -> dict | None:
    try:
        out = subprocess.run(
            ["/usr/sbin/system_profiler", "SPAirPortDataType"],
            capture_output=True, text=True, timeout=20,
        ).stdout
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
            break
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


def _find_wifi_iface_linux() -> str | None:
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.isdir(f"/sys/class/net/{name}/wireless"):
                return name
    except OSError:
        return None
    return None


def _read_wireless_noise_linux(iface: str) -> int | None:
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if line.strip().startswith(iface + ":"):
                    parts = line.split()
                    noise = int(float(parts[3].rstrip(".")))
                    return noise if noise > -256 else None
    except (OSError, ValueError, IndexError):
        return None
    return None


def _wifi_probe_linux() -> dict | None:
    iface = _find_wifi_iface_linux()
    if not iface:
        return None
    iw = shutil.which("iw")
    if not iw:
        return None
    try:
        out = subprocess.run([iw, "dev", iface, "link"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    if not out.strip() or "Not connected" in out:
        return None

    def _s(pat: str) -> str | None:
        m = re.search(pat, out)
        return m.group(1).strip() if m else None

    signal = _s(r"signal:\s*(-?\d+)")
    txrate = _s(r"tx bitrate:\s*([\d.]+)")
    return {
        "ssid": _s(r"SSID:\s*(.+)"),
        "channel": _s(r"freq:\s*(\d+)"),
        "phy_mode": None,
        "rssi_dbm": int(signal) if signal else None,
        "noise_dbm": _read_wireless_noise_linux(iface),
        "tx_rate_mbps": float(txrate) if txrate else None,
    }


# ---- public dispatch --------------------------------------------------------

if _SYS == "Linux":
    read_net_counters = _read_net_counters_linux
    detect_tailscale_iface = _detect_tailscale_iface_linux
    wifi_probe = _wifi_probe_linux
else:  # Darwin (och fallback)
    read_net_counters = _read_net_counters_darwin
    detect_tailscale_iface = _detect_tailscale_iface_darwin
    wifi_probe = _wifi_probe_darwin
