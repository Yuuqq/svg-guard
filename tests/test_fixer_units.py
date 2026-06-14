"""Unit and regression tests for the fixer internals.

The first section exercises pure-Python helpers (no browser needed, so they run
everywhere). The second section is regression coverage for the bug fixes
(B1 percentage width, B2 open/close rect, B3 multi-issue rect) and uses the
Playwright ``page`` fixture.
"""

from pathlib import Path

import pytest

from svg_guard.checker import check_svg
from svg_guard.fixer import (
    _fix_card,
    _parse_len,
    _replace_root_svg_attr,
    _safe_backup,
    fix_svg,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page(browser):
    return browser.new_page(viewport={"width": 1600, "height": 1200})


def _copy_fixture(name: str, tmp: Path) -> Path:
    src = FIXTURES / name
    dst = tmp / name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


# ── pure-Python helper tests ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        ("150", 150.0),
        ("150px", 150.0),
        ("12pt", 16.0),
        ("1.5em", 24.0),
        ("100%", None),
        ("auto", None),
        ("none", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_len(value, expected):
    assert _parse_len(value) == expected


class TestReplaceRootSvgAttr:
    def test_only_touches_root_svg(self):
        # The root <svg width=…> must change; the nested <rect width=…> must not.
        src = '<svg width="800" height="600"><rect width="800" height="600"/></svg>'
        out, ok = _replace_root_svg_attr(src, "width", 20)
        assert ok is True
        assert '<svg width="820"' in out
        assert '<rect width="800"' in out  # nested rect untouched

    def test_returns_false_when_attr_missing(self):
        # Root svg has no width attr — leave it alone rather than inventing one.
        src = '<svg viewBox="0 0 800 600"><rect width="800"/></svg>'
        out, ok = _replace_root_svg_attr(src, "width", 20)
        assert ok is False
        assert out == src

    def test_returns_false_for_non_numeric_attr(self):
        src = '<svg width="100%" height="600"/>'
        out, ok = _replace_root_svg_attr(src, "width", 20)
        assert ok is False
        assert out == src

    def test_returns_false_when_no_svg_tag(self):
        out, ok = _replace_root_svg_attr("not an svg", "width", 20)
        assert ok is False
        assert out == "not an svg"


class TestSafeBackup:
    def test_first_backup_uses_bak(self, tmp_path):
        svg = tmp_path / "a.svg"
        svg.write_text("original", encoding="utf-8")
        _safe_backup(svg)
        assert (tmp_path / "a.svg.bak").read_text(encoding="utf-8") == "original"

    def test_second_backup_uses_numbering(self, tmp_path):
        svg = tmp_path / "a.svg"
        svg.write_text("v2", encoding="utf-8")
        # Pre-create an existing .bak (the true original).
        (tmp_path / "a.svg.bak").write_text("original", encoding="utf-8")
        _safe_backup(svg)
        # Original .bak is preserved; new content lands in .bak.1.
        assert (tmp_path / "a.svg.bak").read_text(encoding="utf-8") == "original"
        assert (tmp_path / "a.svg.bak.1").read_text(encoding="utf-8") == "v2"


class TestFixCardUnits:
    def test_skip_on_percentage_width(self):
        # B1: a percentage width must be skipped with a clear message, not crash.
        attrs = {"x": "10", "y": "10", "width": "100%", "height": "40"}
        content = '<svg><rect x="10" y="10" width="100%" height="40"/></svg>'
        new_content, msg = _fix_card(content, attrs, 30, 0)
        assert new_content == content  # unchanged
        assert msg is not None
        assert "skipped" in msg and 'width="100%"' in msg

    def test_fixes_open_close_rect(self):
        # B2: <rect …></rect> (non-self-closing) must now be fixed.
        attrs = {"x": "10", "y": "10", "width": "150", "height": "40"}
        content = '<svg><rect x="10" y="10" width="150" height="40"></rect></svg>'
        new_content, msg = _fix_card(content, attrs, 30, 0)
        assert msg is not None
        assert 'width="180"' in new_content
        # Closing tag is preserved (the regression: this rect was never matched).
        assert "</rect>" in new_content


# ── Playwright regression tests ────────────────────────────────────────────


class TestOpenCloseRectRegression:
    def test_open_close_rect_is_fixed(self, page, tmp_path):
        # B2: rect written as <rect …></rect> used to never be fixed.
        svg_path = _copy_fixture("open_close_rect.svg", tmp_path)
        result = check_svg(page, svg_path)
        assert not result.ok

        original = svg_path.read_text(encoding="utf-8")
        fix_svg(svg_path, result.issues, backup=False)
        modified = svg_path.read_text(encoding="utf-8")
        assert modified != original
        assert 'width="150"' not in modified  # the original width was widened


class TestMultiIssueRectRegression:
    def test_both_overflows_accounted_for(self, page, tmp_path):
        # B3: two texts overflowing the same rect must not lose the second delta.
        svg_path = _copy_fixture("multi_overflow.svg", tmp_path)
        result = check_svg(page, svg_path)
        assert not result.ok
        text_rect_issues = [i for i in result.issues if i.type == "text_rect"]
        assert len(text_rect_issues) >= 2

        # The expected final width = original + max(expand_w) across the issues.
        # The multi_overflow fixture rect starts at width="200".
        max_expand = max(i.fix.get("expand_w", 0) for i in text_rect_issues)
        expected_width = 200 + max_expand

        fix_svg(svg_path, result.issues, backup=False)
        modified = svg_path.read_text(encoding="utf-8")
        assert f'width="{expected_width}"' in modified


class TestOrphanTextRegression:
    def test_right_overflow_widens_canvas(self, page, tmp_path):
        # B5: orphan text overflowing the right edge should expand the viewBox.
        svg_path = _copy_fixture("orphan_text_right.svg", tmp_path)
        result = check_svg(page, svg_path)
        orphan = [i for i in result.issues if i.type == "text_viewbox"]
        if not orphan:
            pytest.skip("no orphan-text issue on this font")
        original = svg_path.read_text(encoding="utf-8")
        fix_svg(svg_path, result.issues, backup=False)
        modified = svg_path.read_text(encoding="utf-8")
        assert modified != original
        # The viewBox width (300) should have grown.
        assert 'viewBox="0 0 300 ' not in modified
