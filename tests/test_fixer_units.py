"""Unit and regression tests for the fixer internals.

The first section exercises pure-Python helpers (no browser needed, so they run
everywhere). The second section is regression coverage for the bug fixes
(B1 percentage width, B2 open/close rect, B3 multi-issue rect) and uses the
Playwright ``page`` fixture.
"""

from pathlib import Path

import pytest

from svg_guard.checker import check_svg, Issue
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

    def test_dom_index_distinguishes_same_coords(self):
        # Two rects with IDENTICAL x/y/width/height/fill can't be told apart
        # by fingerprint alone. dom_index pinpoints the right one by document
        # position (matching the checker's querySelectorAll order).
        attrs = {"x": "10", "y": "10", "width": "150", "height": "40"}
        content = (
            "<svg>"
            '<rect x="10" y="10" width="150" height="40"/>'  # index 0
            '<rect x="10" y="10" width="150" height="40"/>'  # index 1 (identical)
            "</svg>"
        )
        # Target the SECOND rect (dom_index=1). Without dom_index the fixer
        # would widen the first one and leave the second untouched.
        new_content, msg = _fix_card(content, attrs, 30, 0, dom_index=1)
        assert msg is not None
        # Exactly one rect widened to 180; and it's the second occurrence.
        assert new_content.count('width="180"') == 1
        assert new_content.count('width="150"') == 1
        # The first rect (untouched) still precedes the widened one.
        first_150 = new_content.find('width="150"')
        widened = new_content.find('width="180"')
        assert 0 <= first_150 < widened

    def test_dom_index_zero_targets_first_rect(self):
        attrs = {"x": "10", "y": "10", "width": "150", "height": "40"}
        content = (
            "<svg>"
            '<rect x="10" y="10" width="150" height="40"/>'
            '<rect x="10" y="10" width="150" height="40"/>'
            "</svg>"
        )
        new_content, msg = _fix_card(content, attrs, 30, 0, dom_index=0)
        assert msg is not None
        # Now the FIRST rect is the one widened.
        widened = new_content.find('width="180"')
        remaining = new_content.find('width="150"')
        assert 0 <= widened < remaining

    def test_dom_index_skips_when_attrs_drifted(self):
        # If the file was hand-edited between check and fix, the Nth rect may
        # no longer carry the expected attrs. Refuse rather than widen wrong.
        attrs = {"x": "10", "y": "10", "width": "150", "height": "40"}
        content = '<svg><rect x="999" y="999" width="150" height="40"/></svg>'
        new_content, msg = _fix_card(content, attrs, 30, 0, dom_index=0)
        assert msg is not None
        assert "skipped" in msg
        assert new_content == content  # untouched


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
        # This browser-driven test verifies the END-TO-END path (detect → fix),
        # but its assertion depends on font metrics; the deterministic sibling
        # test below locks the fixer behaviour without a browser.
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

    def test_orphan_text_viewbox_widening_is_deterministic(self, tmp_path):
        # Deterministic regression for the orphan-text fix path: construct the
        # Issue by hand and feed it to fix_svg, so the viewBox widening is
        # verified regardless of which fonts the host has installed. This
        # protects the B5 fix path from silently disappearing if the browser
        # test above starts skipping due to a font change.
        svg = (
            '<svg viewBox="0 0 300 100" width="300" height="100">'
            '<text x="290" y="50">past the right edge</text>'
            "</svg>"
        )
        svg_path = tmp_path / "orphan.svg"
        svg_path.write_text(svg, encoding="utf-8")

        # An orphan text overflowing +50px to the right and +0 to the bottom.
        issue = Issue(
            type="text_viewbox",
            text="past the right edge",
            direction="right",
            svg={"x": 290, "y": 30, "w": 60, "h": 20},
            parent={},
            fix={"expand_viewbox_w": 50, "expand_viewbox_h": 0},
        )
        changes = fix_svg(svg_path, [issue], backup=False)
        assert changes, "fixer should report at least one change"
        modified = svg_path.read_text(encoding="utf-8")
        # viewBox width grew from 300 to 350; root width follows.
        assert 'viewBox="0 0 350 100"' in modified
        assert 'width="350"' in modified

    def test_orphan_text_bottom_overflow_widens_height(self, tmp_path):
        # Same idea, vertical: orphan text below the viewBox bottom edge.
        svg = (
            '<svg viewBox="0 0 300 100" width="300" height="100">'
            '<text x="10" y="95">below the bottom edge</text>'
            "</svg>"
        )
        svg_path = tmp_path / "orphan_bottom.svg"
        svg_path.write_text(svg, encoding="utf-8")

        issue = Issue(
            type="text_viewbox",
            text="below the bottom edge",
            direction="bottom",
            svg={"x": 10, "y": 85, "w": 100, "h": 40},
            parent={},
            fix={"expand_viewbox_w": 0, "expand_viewbox_h": 40},
        )
        changes = fix_svg(svg_path, [issue], backup=False)
        assert changes
        modified = svg_path.read_text(encoding="utf-8")
        assert 'viewBox="0 0 300 140"' in modified
        assert 'height="140"' in modified


# ── fix_svg integration behaviour (no browser) ────────────────────────────


class TestFixSvgBehaviour:
    """End-to-end fix_svg behaviour using hand-built SVGs and Issues.

    No browser involved: these lock down the dispatching, grouping, dry_run
    and backup semantics that the browser-driven regression tests above don't
    cover in isolation.
    """

    def _write(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_dry_run_does_not_write(self, tmp_path):
        svg = (
            '<svg viewBox="0 0 400 200" width="400" height="200">'
            '<rect x="10" y="10" width="150" height="40"/>'
            "</svg>"
        )
        svg_path = self._write(tmp_path, "a.svg", svg)
        before = svg_path.read_text(encoding="utf-8")

        issue = Issue(
            type="text_rect",
            text="x",
            direction="right",
            svg={"x": 10, "y": 10, "w": 200, "h": 40},
            parent={"attrs": {"x": "10", "y": "10", "width": "150", "height": "40"}},
            fix={"expand_w": 30, "expand_h": 0},
        )
        changes = fix_svg(svg_path, [issue], backup=False, dry_run=True)
        assert changes  # dry_run still reports what WOULD change
        # ...but the file on disk must be untouched.
        assert svg_path.read_text(encoding="utf-8") == before

    def test_backup_creates_bak_file(self, tmp_path):
        svg = (
            '<svg viewBox="0 0 400 200" width="400" height="200">'
            '<rect x="10" y="10" width="150" height="40"/>'
            "</svg>"
        )
        svg_path = self._write(tmp_path, "a.svg", svg)

        issue = Issue(
            type="text_rect",
            text="x",
            direction="right",
            svg={"x": 10, "y": 10, "w": 200, "h": 40},
            parent={"attrs": {"x": "10", "y": "10", "width": "150", "height": "40"}},
            fix={"expand_w": 30, "expand_h": 0},
        )
        fix_svg(svg_path, [issue], backup=True)
        assert (tmp_path / "a.svg.bak").exists()
        # Backup holds the ORIGINAL content.
        assert (tmp_path / "a.svg.bak").read_text(encoding="utf-8") == svg

    def test_no_backup_when_disabled(self, tmp_path):
        svg = (
            '<svg viewBox="0 0 400 200" width="400" height="200">'
            '<rect x="10" y="10" width="150" height="40"/>'
            "</svg>"
        )
        svg_path = self._write(tmp_path, "a.svg", svg)
        issue = Issue(
            type="text_rect",
            text="x",
            direction="right",
            svg={"x": 10, "y": 10, "w": 200, "h": 40},
            parent={"attrs": {"x": "10", "y": "10", "width": "150", "height": "40"}},
            fix={"expand_w": 30, "expand_h": 0},
        )
        fix_svg(svg_path, [issue], backup=False)
        assert not (tmp_path / "a.svg.bak").exists()

    def test_mixed_issue_types_applied_together(self, tmp_path):
        # One text_rect (widen rect) + one rect_viewbox (widen viewBox) hitting
        # a DIFFERENT rect: both must be applied in a single fix_svg pass.
        svg = (
            '<svg viewBox="0 0 500 300" width="500" height="300">'
            '<rect x="10" y="10" width="150" height="40"/>'
            '<rect x="10" y="100" width="600" height="40"/>'  # wider than viewBox
            "</svg>"
        )
        svg_path = self._write(tmp_path, "a.svg", svg)

        text_issue = Issue(
            type="text_rect",
            text="x",
            direction="right",
            svg={"x": 10, "y": 10, "w": 200, "h": 40},
            parent={"attrs": {"x": "10", "y": "10", "width": "150", "height": "40"}},
            fix={"expand_w": 30, "expand_h": 0},
        )
        vb_issue = Issue(
            type="rect_viewbox",
            text='rect fill="#fff"',
            direction="right",
            svg={"x": 10, "y": 100, "w": 600, "h": 40},
            parent={
                "domIndex": 1,
                "attrs": {"x": "10", "y": "100", "width": "600", "height": "40"},
            },
            fix={"expand_viewbox_w": 120, "expand_viewbox_h": 0},
        )
        changes = fix_svg(svg_path, [text_issue, vb_issue], backup=False)
        assert changes  # both issue types should produce at least one change
        modified = svg_path.read_text(encoding="utf-8")
        # text_rect widened the 150 rect to 180.
        assert 'rect x="10" y="10" width="180"' in modified
        # rect_viewbox widened the viewBox from 500 to 620.
        assert 'viewBox="0 0 620 300"' in modified

    def test_grouped_issues_collapse_to_one_change(self, tmp_path):
        # Two text_rect issues hitting the SAME rect must produce a single
        # widening to max(expand), not two separate writes.
        svg = (
            '<svg viewBox="0 0 400 200" width="400" height="200">'
            '<rect x="10" y="10" width="200" height="40"/>'
            "</svg>"
        )
        svg_path = self._write(tmp_path, "a.svg", svg)
        attrs = {"x": "10", "y": "10", "width": "200", "height": "40"}
        issues = [
            Issue(
                type="text_rect",
                text="a",
                direction="right",
                svg={"x": 10, "y": 10, "w": 230, "h": 40},
                parent={"attrs": attrs},
                fix={"expand_w": 30, "expand_h": 0},
            ),
            Issue(
                type="text_rect",
                text="b",
                direction="right",
                svg={"x": 10, "y": 10, "w": 260, "h": 40},
                parent={"attrs": attrs},
                fix={"expand_w": 60, "expand_h": 0},
            ),
        ]
        changes = fix_svg(svg_path, issues, backup=False)
        assert changes  # collapsed into a single widening change
        modified = svg_path.read_text(encoding="utf-8")
        # Final width = 200 + max(30, 60) = 260.
        assert 'width="260"' in modified
        assert 'width="230"' not in modified
        assert 'width="290"' not in modified
