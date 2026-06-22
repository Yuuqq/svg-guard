"""Robust SVG read/write helpers.

Centralising I/O here gives every caller:

* ``read_svg``  — tolerates common non-UTF-8 encodings (Shift_JIS / GBK / etc.)
                  that browsers happily render but ``str.read_text("utf-8")``
                  would crash on.
* ``write_text_atomic`` — writes via a sibling temp file then ``os.replace`` so
                  a crash mid-write (disk full, Ctrl+C, permission revoked)
                  can never truncate or wipe the destination.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Bytes-level XML encoding declaration: ``<?xml ... encoding="..."? >``.
_XML_ENC_RE = re.compile(rb'<\?xml[^?]*encoding=["\']([A-Za-z0-9_\-]+)["\']', re.I)


def read_svg(path: Path | str) -> str:
    """Return SVG source as ``str``, tolerating non-UTF-8 encodings.

    Tries UTF-8 first (and strips a BOM if present); on failure falls back to
    the encoding declared in the ``<?xml?>`` prolog, then to a permissive
    latin-1 decode so the file is never a hard crash — the caller (the fixer)
    may decide to skip it, but it will not abort the whole run.
    """
    raw = Path(path).read_bytes()

    # Strip UTF-8 BOM; treat utf-8-sig as utf-8.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    m = _XML_ENC_RE.search(raw[:200])
    if m:
        enc = m.group(1).decode("ascii", errors="replace")
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass

    # Last resort: lossless round-trip (every byte maps 1:1). The fixer's
    # regexes will still work on ASCII tag/attribute names.
    return raw.decode("latin-1")


def write_text_atomic(
    path: Path | str, content: str, *, encoding: str = "utf-8"
) -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a temp file in the same directory, ``fsync``s it, then
    ``os.replace``s over the destination. On any error the temp file is
    removed and the destination is left untouched — so the original file can
    never be half-written or lost, even without a ``.bak`` backup.
    """
    path = Path(path)
    parent = path.parent if path.parent != Path("") else Path(".")
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Includes KeyboardInterrupt — never leave a stray temp file behind.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
