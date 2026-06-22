"""Tests for the HTML report generator.

These are pure-Python (no browser): we construct ``CheckResult`` objects by
hand and assert on the generated HTML string. Covers the previously-untested
``report.py`` module, including the new error-file rendering and the HTML
escaping that prevents broken/XSS'd output.
"""

from __future__ import annotations

from pathlib import Path

from svg_guard.checker import CheckResult, Issue
from svg_guard.report import generate_report


def _issue(text: str = "label overflowing right", direction: str = "right") -> Issue:
    return Issue(
        type="text_rect",
        text=text,
        direction=direction,
        svg={"x": 0, "y": 0, "w": 10, "h": 10},
        parent={},
        fix={"expand_w": 20, "expand_h": 0},
    )


class TestGenerateReport:
    def test_writes_file_and_returns_path(self, tmp_path: Path):
        out = tmp_path / "report.html"
        returned = generate_report({}, out)
        assert returned == out
        assert out.exists()
        assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_empty_results_shows_all_passed(self, tmp_path: Path):
        out = tmp_path / "report.html"
        generate_report({}, out)
        body = out.read_text(encoding="utf-8")
        assert "All SVG files passed" in body

    def test_results_with_issues_rendered(self, tmp_path: Path):
        results = {
            "a.svg": CheckResult(
                path=Path("a.svg"),
                issues=[_issue(text="hello world")],
                viewBox={"w": 400, "h": 200},
            )
        }
        out = tmp_path / "report.html"
        generate_report(results, out)
        body = out.read_text(encoding="utf-8")
        assert "a.svg" in body
        assert "hello world" in body
        assert "text_rect" in body
        # issue count badge
        assert "1 issues" in body

    def test_html_escapes_issue_text(self, tmp_path: Path):
        # An issue text containing markup must not be injected raw.
        evil = "<script>alert(1)</script>"
        results = {
            "a.svg": CheckResult(
                path=Path("a.svg"), issues=[_issue(text=evil)], viewBox=None
            )
        }
        out = tmp_path / "report.html"
        generate_report(results, out)
        body = out.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body  # escaped form present

    def test_html_escapes_filename(self, tmp_path: Path):
        results = {
            "<b>.svg": CheckResult(
                path=Path("<b>.svg"), issues=[_issue()], viewBox=None
            )
        }
        out = tmp_path / "report.html"
        generate_report(results, out)
        body = out.read_text(encoding="utf-8")
        assert "<b>.svg" not in body
        assert "&lt;b&gt;.svg" in body

    def test_error_file_is_rendered_as_error(self, tmp_path: Path):
        # A result that errored (issues=[], error set) must show up as a render
        # error, NOT as "0 issues" and NOT be hidden by the "all passed" branch.
        results = {
            "broken.svg": CheckResult(
                path=Path("broken.svg"), issues=[], viewBox=None, error="boom"
            )
        }
        out = tmp_path / "report.html"
        generate_report(results, out)
        body = out.read_text(encoding="utf-8")
        assert "broken.svg" in body
        assert "render error" in body
        assert "boom" in body
        # The "All passed" banner must not appear when there are errors.
        assert "All SVG files passed" not in body
        # Errors stat should be 1.
        assert '>1</div><div class="stat-label">Errors' in body

    def test_error_only_results_do_not_show_all_passed(self, tmp_path: Path):
        results = {
            "broken.svg": CheckResult(
                path=Path("broken.svg"), issues=[], viewBox=None, error="timeout"
            )
        }
        out = tmp_path / "report.html"
        generate_report(results, out)
        body = out.read_text(encoding="utf-8")
        assert "All SVG files passed" not in body
