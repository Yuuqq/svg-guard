"""Tests for the I/O helpers in _io.py.

Covers read_svg's encoding tolerance and write_text_atomic's crash-safety
guarantee (the destination is never left truncated/half-written).
"""

from __future__ import annotations

import pytest

from svg_guard._io import read_svg, write_text_atomic


class TestReadSvg:
    def test_reads_utf8(self, tmp_path):
        p = tmp_path / "a.svg"
        p.write_text("<svg><text>hello</text></svg>", encoding="utf-8")
        assert read_svg(p) == "<svg><text>hello</text></svg>"

    def test_strips_utf8_bom(self, tmp_path):
        p = tmp_path / "bom.svg"
        p.write_bytes(b"\xef\xbb\xbf<svg/>")
        assert read_svg(p) == "<svg/>"

    def test_reads_cjk_content(self, tmp_path):
        p = tmp_path / "cjk.svg"
        p.write_text("<svg><text>中文标签</text></svg>", encoding="utf-8")
        assert "中文标签" in read_svg(p)

    def test_falls_back_to_declared_encoding(self, tmp_path):
        # An SVG declaring Shift_JIS that is NOT valid UTF-8 must still decode
        # using the declared encoding instead of crashing.
        text = "<svg><text>日本語テスト</text></svg>"
        p = tmp_path / "sjis.svg"
        p.write_bytes(
            b'<?xml version="1.0" encoding="Shift_JIS"?>' + text.encode("shift_jis")
        )
        decoded = read_svg(p)
        assert "日本語テスト" in decoded

    def test_falls_back_to_latin1_as_last_resort(self, tmp_path):
        # Bytes that are neither UTF-8 nor have a declaration: latin-1 round-trips losslessly.
        p = tmp_path / "raw.svg"
        p.write_bytes(b"<svg>\xe9\xe8</svg>")
        decoded = read_svg(p)
        assert decoded.startswith("<svg>")
        assert decoded.endswith("</svg>")

    def test_accepts_path_or_str(self, tmp_path):
        p = tmp_path / "a.svg"
        p.write_text("<svg/>", encoding="utf-8")
        assert read_svg(str(p)) == "<svg/>"


class TestWriteTextAtomic:
    def test_writes_content(self, tmp_path):
        p = tmp_path / "out.svg"
        write_text_atomic(p, "<svg/>")
        assert p.read_text(encoding="utf-8") == "<svg/>"

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "out.svg"
        p.write_text("OLD", encoding="utf-8")
        write_text_atomic(p, "NEW")
        assert p.read_text(encoding="utf-8") == "NEW"

    def test_preserves_original_on_write_failure(self, tmp_path, monkeypatch):
        # The core guarantee: if os.replace fails, the destination is unchanged
        # and no temp file litters the directory.
        p = tmp_path / "out.svg"
        p.write_text("ORIGINAL", encoding="utf-8")

        import svg_guard._io as iomod

        def boom(*a, **kw):
            raise OSError("simulated disk full")

        monkeypatch.setattr(iomod.os, "replace", boom)

        with pytest.raises(OSError):
            write_text_atomic(p, "NEW")
        # Original content survived.
        assert p.read_text(encoding="utf-8") == "ORIGINAL"
        # No stray temp files left behind.
        leftovers = [f for f in tmp_path.iterdir() if f.name.startswith(".out.svg.")]
        assert leftovers == []

    def test_preserves_original_on_exception_during_write(self, tmp_path, monkeypatch):
        # Simulate a failure DURING the write itself (e.g. Ctrl+C mid-write).
        p = tmp_path / "out.svg"
        p.write_text("ORIGINAL", encoding="utf-8")

        import svg_guard._io as iomod

        real_fdopen = iomod.os.fdopen

        def bad_fdopen(fd, *a, **kw):
            f = real_fdopen(fd, *a, **kw)
            original_write = f.write

            def write_then_fail(s):
                original_write(s)
                raise KeyboardInterrupt

            f.write = write_then_fail
            return f

        monkeypatch.setattr(iomod.os, "fdopen", bad_fdopen)

        with pytest.raises(KeyboardInterrupt):
            write_text_atomic(p, "NEW")
        assert p.read_text(encoding="utf-8") == "ORIGINAL"

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "sub" / "dir" / "out.svg"
        write_text_atomic(p, "<svg/>")
        assert p.exists()
        assert p.read_text(encoding="utf-8") == "<svg/>"

    def test_no_temp_file_remains_on_success(self, tmp_path):
        p = tmp_path / "out.svg"
        write_text_atomic(p, "<svg/>")
        leftovers = list(tmp_path.iterdir())
        assert leftovers == [p]
