"""Per-port connection counts from /proc/net/{tcp,tcp6,udp,udp6} — stdlib only, no bytes,
negligible footprint (a few small file reads). Rows are bounded to *service* ports so a busy
host stays a handful of rows:

  dir="in"  : a local LISTEN port (a service we expose). conns = established inbound clients,
              peers = distinct client IPs. Listening ports with 0 clients are still emitted so
              you can see what's open.
  dir="out" : a REMOTE service port we hold outbound connections to (e.g. 443, 6379, 5201),
              grouped by that remote port — so thousands of ephemeral local client ports
              collapse into one row per upstream service. conns / peers as above.

This answers "which ports have incoming / outgoing traffic" without per-flow byte accounting
(that needs SOCK_DIAG netlink or conntrack acct — a follow-up)."""
import time
from collections import defaultdict

from .. import schema

_LISTEN, _ESTAB = "0A", "01"  # /proc/net/tcp state column (hex)
# (proto-label, path): tcp4+tcp6 share the "tcp" label (a :443 service on both = one port).
_PATHS = (("tcp", "/proc/net/tcp"), ("tcp", "/proc/net/tcp6"),
          ("udp", "/proc/net/udp"), ("udp", "/proc/net/udp6"))
_MAX_ROWS = 80  # safety cap so a pathological host never floods the table


def _hostport(hexaddr: str):
    """'0100007F:1F90' -> ('0100007F', 8080). Port is the hex after the last ':'."""
    ip, _, port = hexaddr.rpartition(":")
    return ip, int(port, 16)


def _lines(path: str):
    try:
        with open(path) as f:
            next(f, None)  # skip header
            for line in f:
                yield line.split()
    except OSError:
        return


def collect(conn) -> None:
    ts = time.time()
    listen: dict[str, set] = defaultdict(set)             # proto -> {listening local port}
    raw: dict[tuple, list] = {}
    for proto, path in _PATHS:
        rows = raw[(proto, path)] = list(_lines(path))
        for f in rows:
            if len(f) < 4:
                continue
            _lip, lport = _hostport(f[1])
            # a TCP LISTEN socket, or a UDP socket bound with no peer (rem port 0) ~ a server
            if (proto == "tcp" and f[3] == _LISTEN) or (proto == "udp" and _hostport(f[2])[1] == 0):
                listen[proto].add(lport)

    inbound: dict[tuple, list] = defaultdict(lambda: [0, set()])   # (proto,port) -> [conns,{peer}]
    outbound: dict[tuple, list] = defaultdict(lambda: [0, set()])
    for proto, path in _PATHS:
        for f in raw[(proto, path)]:
            if len(f) < 4:
                continue
            _lip, lport = _hostport(f[1])
            rip, rport = _hostport(f[2])
            if proto == "tcp" and f[3] != _ESTAB:
                continue                       # only count live TCP connections
            if proto == "udp" and rport == 0:
                continue                       # the bound server socket itself, not a flow
            if lport in listen[proto]:         # client -> our service port (inbound)
                e = inbound[(proto, lport)]
            else:                              # us -> remote service port (outbound)
                e = outbound[(proto, rport)]
            e[0] += 1
            e[1].add(rip)

    rows = []
    for proto, ports in listen.items():
        for port in ports:
            c, peers = inbound.get((proto, port), (0, ()))
            rows.append({"ts": ts, "proto": proto, "dir": "in", "port": port,
                         "conns": c, "peers": len(peers), "listening": 1})
    for (proto, port), (c, peers) in outbound.items():
        rows.append({"ts": ts, "proto": proto, "dir": "out", "port": port,
                     "conns": c, "peers": len(peers), "listening": 0})
    # keep all listening services + the busiest outbound ports if we somehow exceed the cap
    rows.sort(key=lambda r: (r["dir"] == "out", -r["conns"]))
    schema.insert(conn, "port_samples", rows[:_MAX_ROWS])
