"""Per-port connection counts + byte volume from the kernel, stdlib only, no root needed.

Two cheap sources:
  - /proc/net/{tcp,tcp6,udp,udp6}: which ports listen + how many connections (conns/peers).
  - SOCK_DIAG netlink (NETLINK_INET_DIAG): per-socket tcp_info -> bytes_acked (sent) and
    bytes_received, which we sum per port. These are CUMULATIVE per connection (since it
    opened), so a port's bytes = total moved by its currently-open connections (a gauge that
    drops as connections close, not a clean rate). tcp_info is readable unprivileged.

Rows are bounded to *service* ports so a busy host stays a handful of rows:
  dir="in"  : a local LISTEN port (a service we expose). conns/bytes are inbound clients.
  dir="out" : a REMOTE service port we connect to (443, 6379, 5201 ...), grouped by that
              remote port, so thousands of ephemeral local client ports collapse to one row."""
import socket
import struct
import time
from collections import defaultdict

from .. import schema

_LISTEN, _ESTAB = "0A", "01"  # /proc/net/tcp state column (hex)
_PATHS = (("tcp", "/proc/net/tcp"), ("tcp", "/proc/net/tcp6"),
          ("udp", "/proc/net/udp"), ("udp", "/proc/net/udp6"))
_MAX_ROWS = 80

# SOCK_DIAG (inet_diag) constants
_NETLINK_INET_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_INET_DIAG_INFO = 2
_TCP_ESTABLISHED = 1


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


def _tcp_diag_bytes(family: int):
    """Yield (local_port, remote_port, bytes_sent, bytes_recv) for established TCP sockets of
    `family`, via a SOCK_DIAG dump. bytes_* come from tcp_info (acked/received). Best-effort:
    on any error (old kernel, blocked netlink) it yields nothing and the caller degrades to
    conn-counts only."""
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, _NETLINK_INET_DIAG)
    except OSError:
        return
    try:
        s.settimeout(2.0)
        # inet_diag_req_v2: family, protocol, ext(bitmask), pad, states(bitmask) + sockid(48)
        ext = 1 << (_INET_DIAG_INFO - 1)
        states = 1 << _TCP_ESTABLISHED
        sockid = struct.pack("=HH16s16sI", 0, 0, b"\0" * 16, b"\0" * 16, 0) + struct.pack("=II", 0, 0)
        req = struct.pack("=BBBBI", family, socket.IPPROTO_TCP, ext, 0, states) + sockid
        msg = struct.pack("=IHHII", 16 + len(req), _SOCK_DIAG_BY_FAMILY, 0x0301, 1, 0) + req
        s.send(msg)
        while True:
            buf = s.recv(65536)
            off = 0
            while off + 16 <= len(buf):
                mlen, mtype, _flags, _seq, _pid = struct.unpack_from("=IHHII", buf, off)
                if mlen < 16 or off + mlen > len(buf):
                    return
                if mtype in (2, 3):  # NLMSG_ERROR / NLMSG_DONE
                    return
                sport, dport = struct.unpack_from("=HH", buf, off + 16 + 4)  # idiag_sockid ports (BE)
                sport, dport = socket.ntohs(sport), socket.ntohs(dport)
                sent = recv = None
                a = off + 16 + 72  # past nlmsghdr(16) + inet_diag_msg(72) -> attributes
                while a + 4 <= off + mlen:
                    rlen, rtype = struct.unpack_from("=HH", buf, a)
                    if rlen < 4:
                        break
                    if rtype == _INET_DIAG_INFO:
                        info = buf[a + 4:a + rlen]
                        if len(info) >= 136:  # bytes_acked@120, bytes_received@128 (8 bytes each)
                            sent, recv = struct.unpack_from("=QQ", info, 120)
                    a += (rlen + 3) & ~3
                yield sport, dport, sent, recv
                off += (mlen + 3) & ~3
    except OSError:
        return
    finally:
        s.close()


def collect(conn) -> None:
    ts = time.time()
    listen: dict[str, set] = defaultdict(set)
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
                continue
            if proto == "udp" and rport == 0:
                continue
            e = inbound[(proto, lport)] if lport in listen[proto] else outbound[(proto, rport)]
            e[0] += 1
            e[1].add(rip)

    # byte volume per port (TCP only): sum tcp_info bytes over each port's open connections,
    # classified inbound/outbound by the same listening-port set as the conn counts above.
    bytes_in: dict[int, list] = defaultdict(lambda: [0, 0])   # port -> [sent, recv]
    bytes_out: dict[int, list] = defaultdict(lambda: [0, 0])
    tcp_listen = listen["tcp"]
    for fam in (socket.AF_INET, socket.AF_INET6):
        for lport, rport, sent, recv in _tcp_diag_bytes(fam):
            if sent is None:
                continue
            tgt = bytes_in[lport] if lport in tcp_listen else bytes_out[rport]
            tgt[0] += sent
            tgt[1] += recv

    rows = []
    for port in listen["tcp"]:
        c, peers = inbound.get(("tcp", port), (0, ()))
        b = bytes_in.get(port, (None, None))
        rows.append({"ts": ts, "proto": "tcp", "dir": "in", "port": port, "conns": c,
                     "peers": len(peers), "listening": 1, "bytes_sent": b[0], "bytes_recv": b[1]})
    for port in listen["udp"]:
        c, peers = inbound.get(("udp", port), (0, ()))
        rows.append({"ts": ts, "proto": "udp", "dir": "in", "port": port, "conns": c,
                     "peers": len(peers), "listening": 1, "bytes_sent": None, "bytes_recv": None})
    for (proto, port), (c, peers) in outbound.items():
        b = bytes_out.get(port, (None, None)) if proto == "tcp" else (None, None)
        rows.append({"ts": ts, "proto": proto, "dir": "out", "port": port, "conns": c,
                     "peers": len(peers), "listening": 0, "bytes_sent": b[0], "bytes_recv": b[1]})
    rows.sort(key=lambda r: (r["dir"] == "out", -(r["bytes_sent"] or 0), -r["conns"]))
    schema.insert(conn, "port_samples", rows[:_MAX_ROWS])
