"""Hub tolerates a client that hangs up mid-response (dashboard reload / fetch abort) instead of
dumping a BrokenPipe traceback per cancelled request."""

import pytest

from smokemon import hub


class _DeadWFile:
    """A wfile whose write fails as if the peer closed the socket."""

    def __init__(self, exc):
        self.exc = exc

    def write(self, _data):
        raise self.exc


def _bare_handler(exc):
    h = hub.Handler.__new__(hub.Handler)  # skip __init__ (no real socket)
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = _DeadWFile(exc)
    h.close_connection = False
    return h


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"),
                                 ConnectionResetError(), ConnectionAbortedError()])
def test_write_swallows_client_disconnect(exc):
    h = _bare_handler(exc)
    h._write(200, b"payload", "application/json")  # must not raise
    assert h.close_connection is True


def test_send_json_swallows_client_disconnect():
    h = _bare_handler(BrokenPipeError(32, "Broken pipe"))
    h._send(500, {"error": "internal error"})  # the exact second-write that used to traceback
    assert h.close_connection is True
