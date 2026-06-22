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
    _expand_viewbox,
    _fix_card,
    _parse_len,
    _safe_backup,
    _sync_root_svg_dim,
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


class TestSyncRootSvgDim:
    def test_adds_delta_to_existing_numeric_attr(self):
        # The root <svg width=…> must change; the nested <rect width=…> must not.
        src = '<svg width="800" height="600"><rect width="800" height="600"/></svg>'
        out, mode = _sync_root_svg_dim(src, "width", 20, fallback_value=None)
        assert mode == "add"
        assert '<svg width="820"' in out
        assert '<rect width="800"' in out  # nested rect untouched

    def test_injects_attr_when_missing(self):
        # Root svg has no width — inject it from the new viewBox dim so the
        # canvas grows with the viewBox instead of squashing the diagram.
        src = '<svg viewBox="0 0 800 600"><rect width="800"/></svg>'
        out, mode = _sync_root_svg_dim(src, "width", 20, fallback_value=820)
        assert mode == "inject"
        assert 'width="820"' in out
        assert out != src

    def test_skips_when_missing_and_no_fallback(self):
        # No width attr and we don't know the viewBox dim → leave it alone.
        src = '<svg viewBox="0 0 800 600"><rect width="800"/></svg>'
        out, mode = _sync_root_svg_dim(src, "width", 20, fallback_value=None)
        assert mode == "skip"
        assert out == src

    def test_skips_for_non_numeric_attr(self):
        # width="100%" can't be incremented — must not be invented over either.
        src = '<svg width="100%" height="600"/>'
        out, mode = _sync_root_svg_dim(src, "width", 20, fallback_value=820)
        assert mode == "skip"
        assert out == src

    def test_skips_when_no_svg_tag(self):
        out, mode = _sync_root_svg_dim("not an svg", "width", 20, fallback_value=None)
        assert mode == "skip"
        assert out == "not an svg"


class TestExpandViewbox:
    def test_handles_single_quoted_viewbox(self):
        # Single-quoted viewBox must be rewritten, not silently skipped.
        src = "<svg viewBox='0 0 400 200' width='400' height='200'><rect/></svg>"
        out, msg = _expand_viewbox(src, 50, 0)
        assert msg is not None
        assert "viewBox='0 0 450 200'" in out  # quote style preserved, w grown
        assert "width='450'" in out or 'width="450"' in out

    def test_handles_double_quoted_viewbox(self):
        src = '<svg viewBox="0 0 400 200" width="400" height="200"><rect/></svg>'
        out, msg = _expand_viewbox(src, 0, 30)
        assert msg is not None
        assert 'viewBox="0 0 400 230"' in out
        assert 'height="230"' in out

    def test_injects_width_when_root_has_none(self):
        # Root svg without width/height: canvas must be injected from the new
        # viewBox so the diagram doesn't silently shrink.
        src = '<svg viewBox="0 0 400 200"><rect/></svg>'
        out, msg = _expand_viewbox(src, 60, 0)
        assert msg is not None
        assert 'viewBox="0 0 460 200"' in out
        assert 'width="460"' in out
        assert "injected" in msg

    def test_returns_none_for_zero_delta(self):
        src = '<svg viewBox="0 0 400 200"/>'
        out, msg = _expand_viewbox(src, 0, 0)
        assert msg is None
        assert out == src

    def test_skips_malformed_viewbox(self):
        # Non-4-segment viewBox must be reported as skipped, not crash.
        src = '<svg viewBox="0 0 400" width="400"/>'
        out, msg = _expand_viewbox(src, 50, 0)
        assert msg is not None
        assert "skipped" in msg
        assert 'viewBox="0 0 400"' in out  # untouched

    def test_skips_non_numeric_viewbox(self):
        src = '<svg viewBox="0 0 abc 200" width="400"/>'
        out, msg = _expand_viewbox(src, 50, 0)
        assert msg is not None
        assert "skipped" in msg


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

    def test_fingerprint_matches_exact_attrs_not_substring(self):
        # A7: x="10" must NOT substring-match x="100" or transform="translate(10,…)".
        # Here the target is the x="10" rect; a decoy with x="100" must be ignored.
        attrs = {"x": "10", "y": "10", "width": "150", "height": "40"}
        content = (
            "<svg>"
            '<rect x="100" y="10" width="150" height="40"/>'
            '<rect x="10" y="10" width="150" height="40"/>'
            "</svg>"
        )
        new_content, msg = _fix_card(content, attrs, 30, 0)
        assert msg is not None
        # Only the x="10" rect's width becomes 180; the x="100" decoy stays 150.
        assert new_content.count('width="180"') == 1
        assert new_content.count('width="150"') == 1

    def test_not_confused_by_transform_substring(self):
        # A7: a transform="translate(10,10)" must not make a different rect
        # look like an x="10" y="10" match.
        attrs = {"x": "10", "y": "10", "width": "150", "height": "40"}
        content = (
            "<svg>"
            '<rect x="200" y="200" width="150" height="40" transform="translate(10,10)"/>'
            '<rect x="10" y="10" width="150" height="40"/>'
            "</svg>"
        )
        new_content, msg = _fix_card(content, attrs, 30, 0)
        assert msg is not None
        # The transform decoy keeps width="150"; only the real target widens.
        assert new_content.count('width="180"') == 1
        assert new_content.count('width="150"') == 1

    def test_returns_none_when_no_rect_matches(self):
        # A7: attrs pointing to a non-existent rect must return (content, None).
        attrs = {"x": "999", "y": "999", "width": "150", "height": "40"}
        content = '<svg><rect x="10" y="10" width="150" height="40"/></svg>'
        new_content, msg = _fix_card(content, attrs, 30, 0)
        assert msg is None
        assert new_content == content


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
