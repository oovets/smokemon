"""TCP liveness probe: spec parsing + connect/read-bytes liveness against a local socket."""

import socket
import threading

from smokemon.probes import tcpcheck


def test_parse():
    assert tcpcheck._parse("videofeed=127.0.0.1:5000") == ("videofeed", "127.0.0.1", 5000, None)
    assert tcpcheck._parse("feed=10.0.0.1:9000:512") == ("feed", "10.0.0.1", 9000, 512)
    assert tcpcheck._parse("nonsense") is None          # no '='
    assert tcpcheck._parse("x=hostonly") is None         # no port
    assert tcpcheck._parse("x=h:notaport") is None       # bad port


def _serve_once(send_bytes: bytes | None):
    """A one-shot listener; returns (port, started_event). If send_bytes is None it accepts then
    stays silent (simulates a stalled feed); otherwise it sends the bytes and closes."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
            if send_bytes:
                conn.sendall(send_bytes)
            else:
                import time as _t  # hold the socket open without sending (stall)
                _t.sleep(1.5)
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def test_check_up_when_bytes_flow():
    port = _serve_once(b"\x47" * 188)   # an MPEG-TS-ish packet
    ok, latency, nbytes, detail = tcpcheck._check("127.0.0.1", port, 1, timeout=2)
    assert ok and nbytes >= 1 and detail == "ok"


def test_check_down_when_connection_refused():
    # nothing listening on this port -> connect fails
    ok, _, _, detail = tcpcheck._check("127.0.0.1", 1, 1, timeout=1)
    assert not ok and "connect failed" in detail


def test_check_down_when_socket_open_but_no_data():
    # the "stalled feed" case: socket accepts but never sends -> read timeout -> down
    port = _serve_once(None)
    ok, _, nbytes, detail = tcpcheck._check("127.0.0.1", port, 1, timeout=0.5)
    assert not ok and nbytes == 0 and "no data" in detail
