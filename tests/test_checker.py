"""Tests for the checker module."""

from pathlib import Path

import pytest

from svg_guard.checker import check_svg

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
