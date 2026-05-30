"""Zero-footprint UX helpers: NO_COLOR / tty colour gating, ascii glyph fallback for
non-utf8 terminals, and coloured verdict/incident output. All pure-stdlib, no rendering."""

import io

from smokemon import report


class _TTY(io.StringIO):
    def isatty(self):
        return True


def test_use_color_gating(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert report.use_color(_TTY()) is True
    assert report.use_color(io.StringIO()) is False         # not a tty (pipe/file)
    assert report.use_color(_TTY(), disable=True) is False  # explicit --no-color


def test_no_color_env_disables_even_on_tty(monkeypatch):
    # no-color.org: present regardless of value -> never colour.
    monkeypatch.setenv("NO_COLOR", "")
    assert report.use_color(_TTY()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert report.use_color(_TTY()) is False


def test_unicode_ok_detection():
    assert report.unicode_ok(type("S", (), {"encoding": "UTF-8"})()) is True
    assert report.unicode_ok(type("S", (), {"encoding": "ascii"})()) is False
    assert report.unicode_ok(type("S", (), {"encoding": None})()) is False


def test_ascii_glyph_fallback():
    report.ASCII = True
    try:
        assert report.sparkline([0, 1, 2, 3, 4, 5, 6, 7]) == report._SPARK_ASCII
        assert "▁" not in report.sparkline([0, 5, 9])
        assert report._dot("down", color=False) == "x"
        assert report._dot("stale", color=False) == "."
    finally:
        report.ASCII = False
    assert report.sparkline([0, 7])[0] == "▁"   # default is unicode again


def test_color_wraps_but_preserves_text():
    assert report._color_verdict("healthy", True) == "\x1b[32mhealthy\x1b[0m"
    assert report._color_verdict("recovered", True).startswith("\x1b[33m")
    assert report._color_verdict("healthy", False) == "healthy"
    out = report._color_verdict("ISP OUTAGE", True)        # active incident -> red
    assert "ISP OUTAGE" in out and out.startswith("\x1b[31m")
