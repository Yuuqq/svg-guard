"""Tests for the fixer module."""

import tempfile
from pathlib import Path

import pytest

from svg_guard.checker import Issue, check_svg
from svg_guard.fixer import fix_svg

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page(browser):
    return browser.new_page(viewport={"width": 1600, "height": 1200})


def _copy_fixture(name: str, tmp: Path) -> Path:
    src = FIXTURES / name
    dst = tmp / name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


class TestFixViewbox:
    def test_viewbox_overflow_gets_fixed(self, page, tmp_path):
        svg_path = _copy_fixture("viewbox_overflow.svg", tmp_path)
        result = check_svg(page, svg_path)
        assert not result.ok

        changes = fix_svg(svg_path, result.issues, backup=False)
        assert len(changes) > 0
        assert any("viewBox" in c for c in changes)

        result2 = check_svg(page, svg_path)
        assert len(result2.issues) < len(result.issues)


class TestFixCard:
    def test_card_fix_applied(self, page, tmp_path):
        svg_path = _copy_fixture("text_overflow.svg", tmp_path)
        result = check_svg(page, svg_path)
        assert not result.ok

        original = svg_path.read_text(encoding="utf-8")
        changes = fix_svg(svg_path, result.issues, backup=False)
        assert len(changes) > 0

        modified = svg_path.read_text(encoding="utf-8")
        assert original != modified

    def test_card_fix_reduces_overflow(self, page, tmp_path):
        svg_path = _copy_fixture("text_overflow.svg", tmp_path)
        result1 = check_svg(page, svg_path)
        before_count = len(result1.issues)

        fix_svg(svg_path, result1.issues, backup=False)
        result2 = check_svg(page, svg_path)
        assert len(result2.issues) <= before_count


class TestFixBackup:
    def test_backup_created_by_default(self, page, tmp_path):
        svg_path = _copy_fixture("text_overflow.svg", tmp_path)
        result = check_svg(page, svg_path)

        fix_svg(svg_path, result.issues, backup=True)
        assert svg_path.with_suffix(".svg.bak").exists()

    def test_no_backup_when_disabled(self, page, tmp_path):
        svg_path = _copy_fixture("text_overflow.svg", tmp_path)
        result = check_svg(page, svg_path)

        fix_svg(svg_path, result.issues, backup=False)
        assert not svg_path.with_suffix(".svg.bak").exists()

    def test_dry_run_does_not_modify_file(self, page, tmp_path):
        svg_path = _copy_fixture("text_overflow.svg", tmp_path)
        original = svg_path.read_text(encoding="utf-8")
        result = check_svg(page, svg_path)

        fix_svg(svg_path, result.issues, dry_run=True)
        assert svg_path.read_text(encoding="utf-8") == original
