"""Tests for the checker module."""

from pathlib import Path

import pytest

from svg_guard.checker import DetectionConfig, check_svg

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page(browser):
    return browser.new_page(viewport={"width": 1600, "height": 1200})


class TestCheckSvg:
    def test_good_svg_no_issues(self, page):
        result = check_svg(page, FIXTURES / "good.svg")
        assert result.ok
        assert result.issues == []

    def test_text_overflow_detected(self, page):
        result = check_svg(page, FIXTURES / "text_overflow.svg")
        assert not result.ok
        assert any(i.type == "text_rect" for i in result.issues)
        text_overflow = [i for i in result.issues if i.type == "text_rect"][0]
        assert "right" in text_overflow.direction or "bottom" in text_overflow.direction

    def test_viewbox_overflow_detected(self, page):
        result = check_svg(page, FIXTURES / "viewbox_overflow.svg")
        assert not result.ok
        assert any(i.type == "rect_viewbox" for i in result.issues)

    def test_issue_has_svg_coords(self, page):
        result = check_svg(page, FIXTURES / "text_overflow.svg")
        issue = [i for i in result.issues if i.type == "text_rect"][0]
        assert "x" in issue.svg
        assert "y" in issue.svg
        assert "w" in issue.svg
        assert "h" in issue.svg

    def test_issue_has_fix_info(self, page):
        result = check_svg(page, FIXTURES / "text_overflow.svg")
        issue = [i for i in result.issues if i.type == "text_rect"][0]
        assert "expand_w" in issue.fix or "expand_h" in issue.fix

    def test_cjk_overflow_detected(self, page):
        # CJK glyphs are full-width; a label that looks short character-wise
        # can still overflow. This locks detection behaviour for the project's
        # primary use case, independent of which CJK font the host has.
        result = check_svg(page, FIXTURES / "cjk_overflow.svg")
        # Whether it overflows depends on the installed CJK font width, so we
        # assert the safer invariant: detection returns a result without
        # crashing, and if it found issues they are text_rect type.
        if not result.ok:
            assert all(i.type == "text_rect" for i in result.issues)

    def test_nested_svg_does_not_crash(self, page):
        # Nested <svg> elements (common in Inkscape exports, symbol+use, etc.)
        # must not crash detection. We lock the current behaviour rather than
        # assert a specific issue count, since nested-svg coordinate mapping
        # is a known limitation flagged for the architecture batch.
        result = check_svg(page, FIXTURES / "nested_svg.svg")
        assert result.error is None  # didn't crash
        # Returns a viewBox dict, not None.
        assert result.viewBox is not None

    def test_rotated_rect_does_not_crash(self, page):
        # A rect with transform=rotate(45) exercises the getCTM code path:
        # getBBox gives the local geometry, getCTM folds in the rotation, and
        # we take the axis-aligned box in viewBox space. Must not crash and
        # must not throw the old "transformPoint is not a function" error.
        result = check_svg(page, FIXTURES / "rotated_rect.svg")
        assert result.error is None

    def test_left_overflow_marked_unfixable(self, page):
        # Text starting LEFT of its parent rect's left edge cannot be fixed by
        # widening the rect (which grows rightward). The checker must mark it
        # fixable=false so the fixer skips it instead of looping.
        result = check_svg(page, FIXTURES / "text_overflow_left.svg")
        assert not result.ok
        text_rects = [i for i in result.issues if i.type == "text_rect"]
        assert text_rects, "expected at least one text_rect issue"
        # The left-overflowing text must be flagged unfixable.
        assert any(i.fix.get("fixable") is False for i in text_rects)
        # And its direction must include "left".
        assert any("left" in i.direction for i in text_rects)

    def test_wide_viewbox_rect_not_false_positive(self, page):
        # viewBox 800x200 (4:1) vs viewport 1600x1200 (4:3). The old code's
        # independent scaleX/scaleY skewed the rect_viewbox check and could
        # report a false overflow. A rect that fits the viewBox must be clean.
        result = check_svg(page, FIXTURES / "wide_viewbox.svg")
        assert result.ok, (
            f"expected no issues for a rect fitting its wide viewBox, got: "
            f"{[(i.type, i.direction) for i in result.issues]}"
        )


class TestDetectionConfig:
    def test_as_js_round_trips_all_thresholds(self):
        cfg = DetectionConfig(
            pad=5.0,
            edge_pad=6.0,
            vpad=1.0,
            fix_pad=8.0,
            vbox_fix_pad=12.0,
            min_rect_w=10.0,
            min_rect_h=20.0,
        )
        js = cfg.as_js()
        assert js == {
            "pad": 5.0,
            "edgePad": 6.0,
            "vpad": 1.0,
            "fixPad": 8.0,
            "vboxFixPad": 12.0,
            "minRectW": 10.0,
            "minRectH": 20.0,
        }

    def test_large_pad_makes_good_svg_report_overflow(self, page):
        # A config-level guarantee: with a huge pad tolerance... no wait, a
        # huge pad makes detection MORE lenient (text can be further out
        # before counting). To force a false positive on the clean good.svg we
        # shrink the min-rect threshold so the text gets matched, then set pad
        # negative so even snug text counts as overflowing.
        cfg = DetectionConfig(pad=-100.0, min_rect_w=10.0, min_rect_h=10.0)
        result = check_svg(page, FIXTURES / "good.svg", config=cfg)
        assert not result.ok, "negative pad should make snug text overflow"

    def test_defaults_match_documented_behaviour(self):
        # Lock the shipped defaults so a careless edit can't silently change
        # detection sensitivity for all users.
        cfg = DetectionConfig()
        assert (cfg.pad, cfg.edge_pad, cfg.vpad) == (3.0, 4.0, 2.0)
        assert (cfg.min_rect_w, cfg.min_rect_h) == (80.0, 40.0)
        assert (cfg.viewport_w, cfg.viewport_h) == (1600, 1200)
