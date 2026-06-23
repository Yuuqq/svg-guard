"""Tests for the HTML and JSON report generators.

These are pure-Python (no browser): we construct ``CheckResult`` objects by
hand and assert on the generated output. Covers the HTML rendering (incl. the
error-file rendering and HTML escaping that prevents broken/XSS'd output) and
the JSON serializer used by ``check --json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from svg_guard.checker import CheckResult, Issue
from svg_guard.report import generate_report, write_json_report


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


class TestWriteJsonReport:
    def _issue(self) -> Issue:
        return Issue(
            type="text_rect",
            text="hello",
            direction="right",
            svg={"x": 0, "y": 0, "w": 10, "h": 10},
            parent={},
            fix={"expand_w": 5, "expand_h": 0},
        )

    def test_writes_valid_json(self, tmp_path: Path):
        out = tmp_path / "r.json"
        results = {
            "a.svg": CheckResult(
                path=Path("a.svg"), issues=[self._issue()], viewBox=None
            )
        }
        write_json_report(results, out)
        assert out.exists()
        report = json.loads(out.read_text(encoding="utf-8"))
        assert "a.svg" in report
        assert report["a.svg"][0]["type"] == "text_rect"

    def test_clean_files_omitted(self, tmp_path: Path):
        out = tmp_path / "r.json"
        results = {
            "good.svg": CheckResult(path=Path("good.svg"), issues=[], viewBox=None),
            "bad.svg": CheckResult(
                path=Path("bad.svg"), issues=[self._issue()], viewBox=None
            ),
        }
        write_json_report(results, out)
        report = json.loads(out.read_text(encoding="utf-8"))
        assert "good.svg" not in report
        assert "bad.svg" in report

    def test_error_file_serialized_as_error_object(self, tmp_path: Path):
        # A render error must appear as {"error": "..."}, NOT an empty issue
        # list (which would look like "clean" to CI tooling).
        out = tmp_path / "r.json"
        results = {
            "broken.svg": CheckResult(
                path=Path("broken.svg"), issues=[], viewBox=None, error="boom"
            )
        }
        write_json_report(results, out)
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["broken.svg"] == {"error": "boom"}

    def test_cjk_text_round_trips_through_json(self, tmp_path: Path):
        # ensure_ascii=False must hold so CJK issue text survives a round trip.
        out = tmp_path / "r.json"
        results = {
            "a.svg": CheckResult(
                path=Path("a.svg"),
                issues=[
                    Issue(
                        type="text_rect",
                        text="中文标签溢出",
                        direction="right",
                        svg={"x": 0, "y": 0, "w": 1, "h": 1},
                        parent={},
                        fix={},
                    )
                ],
                viewBox=None,
            )
        }
        write_json_report(results, out)
        raw = out.read_text(encoding="utf-8")
        assert "中文标签溢出" in raw  # not \u-escaped
        report = json.loads(raw)
        assert report["a.svg"][0]["text"] == "中文标签溢出"


class TestVisualPreview:
    """The HTML report's per-file visual preview: inlined SVG + red-box overlay.

    These need a real file on disk (the preview reads it to inline), so they
    write a tiny SVG into tmp_path. No browser involved.
    """

    _SVG = (
        '<svg viewBox="0 0 400 200" width="400" height="200" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<rect x="50" y="50" width="200" height="80" fill="#eee"/>'
        '<text x="60" y="90">hi</text>'
        "</svg>"
    )

    def _result(self, path: Path) -> CheckResult:
        issue = Issue(
            type="text_rect",
            text="hi",
            direction="right",
            svg={"x": 60, "y": 75, "w": 220, "h": 30},  # overflows the 200-wide rect
            parent={"svg": {"x": 50, "y": 50, "w": 200, "h": 80}},
            fix={"expand_w": 30, "expand_h": 0, "fixable": True},
        )
        return CheckResult(
            path=path,
            issues=[issue],
            viewBox={"w": 400, "h": 200},
        )

    def test_preview_contains_inlined_svg_image(self, tmp_path: Path):
        svg = tmp_path / "a.svg"
        svg.write_text(self._SVG, encoding="utf-8")
        out = tmp_path / "report.html"
        generate_report({"a.svg": self._result(svg)}, out)
        body = out.read_text(encoding="utf-8")
        # The source is inlined as a base64 data URI image.
        assert 'src="data:image/svg+xml;base64,' in body

    def test_preview_draws_red_box_for_each_issue(self, tmp_path: Path):
        svg = tmp_path / "a.svg"
        svg.write_text(self._SVG, encoding="utf-8")
        out = tmp_path / "report.html"
        generate_report({"a.svg": self._result(svg)}, out)
        body = out.read_text(encoding="utf-8")
        # An overlay SVG exists with the file's viewBox.
        assert 'class="overlay"' in body
        assert 'viewBox="0 0 400 200"' in body
        # A red box with data-idx=1 for the first issue, at its measured coords.
        assert 'class="box" data-idx="1"' in body
        assert 'x="60"' in body and 'width="220"' in body
        # The parent rect is drawn dashed.
        assert 'class="parent"' in body

    def test_issue_rows_carry_index_linking_to_overlay(self, tmp_path: Path):
        svg = tmp_path / "a.svg"
        svg.write_text(self._SVG, encoding="utf-8")
        out = tmp_path / "report.html"
        generate_report({"a.svg": self._result(svg)}, out)
        body = out.read_text(encoding="utf-8")
        # Row and box share data-idx="1" so the hover-link script can pair them.
        assert 'class="issue-row" data-idx="1"' in body
        assert 'data-idx="1"' in body  # box
        # The linking script is present.
        assert "<script>" in body

    def test_no_preview_when_viewbox_missing(self, tmp_path: Path):
        # If neither the measured viewBox nor the source has one, there is no
        # coordinate system to align an overlay to — skip the preview entirely
        # and just render the issue list.
        svg = tmp_path / "a.svg"
        svg.write_text("<svg/>", encoding="utf-8")  # no viewBox
        result = CheckResult(
            path=svg,
            issues=[
                Issue(
                    type="text_rect",
                    text="x",
                    direction="right",
                    svg={"x": 1, "y": 2, "w": 3, "h": 4},
                    parent={},
                    fix={},
                )
            ],
            viewBox=None,
        )
        out = tmp_path / "report.html"
        generate_report({"a.svg": result}, out)
        body = out.read_text(encoding="utf-8")
        assert '<div class="preview">' not in body  # no preview block rendered
        # Issue list still rendered.
        assert "text_rect" in body

    def test_large_svg_falls_back_to_schematic(self, tmp_path: Path):
        # A source bigger than the inline cap is NOT inlined as an image, but
        # the coordinate overlay is still drawn from the measured data.
        svg = tmp_path / "big.svg"
        # Pad the SVG well past _MAX_PREVIEW_BYTES with a huge comment.
        pad = "<!-- " + ("x" * 210_000) + " -->"
        svg.write_text(self._SVG.replace("</svg>", pad + "</svg>"), encoding="utf-8")
        out = tmp_path / "report.html"
        generate_report({"big.svg": self._result(svg)}, out)
        body = out.read_text(encoding="utf-8")
        assert "data:image/svg+xml;base64" not in body  # not inlined
        assert "schematic only" in body  # fallback note shown
        assert 'class="box" data-idx="1"' in body  # overlay still drawn
