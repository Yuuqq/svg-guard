"""Tests for check_directory's error handling and side effects.

Two flavours:

* Pure logic (missing dir, empty dir) — call check_directory in-process; these
  raise before Playwright is ever touched.
* Playwright-driven (corrupt SVG, json_out, string path) — run the real CLI
  in a SUBPROCESS. svg-guard uses sync_playwright, and pytest-playwright
  (loaded by the browser-driven tests in this suite) keeps an asyncio loop
  alive that conflicts with a second sync_playwright in the same process.
  A subprocess isolates Playwright cleanly and tests the real entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from svg_guard.checker import check_directory, collect_svgs

FIXTURES = Path(__file__).parent / "fixtures"


class TestCheckDirectoryPureLogic:
    """Branches that raise before any browser starts — safe in-process."""

    def test_missing_dir_raises(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(FileNotFoundError):
            check_directory(missing)

    def test_empty_dir_raises(self, tmp_path):
        # Directory exists but has no .svg files.
        with pytest.raises(FileNotFoundError):
            check_directory(tmp_path)

    def test_collect_single_file(self, tmp_path):
        svg = tmp_path / "a.svg"
        svg.write_text("<svg/>", encoding="utf-8")
        found = collect_svgs([svg])
        assert [key for _, key in found] == ["a.svg"]

    def test_collect_rejects_non_svg(self, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("nope", encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            collect_svgs([txt])

    def test_collect_recursive_and_ignore(self, tmp_path):
        (tmp_path / "top.svg").write_text("<svg/>", encoding="utf-8")
        nested = tmp_path / "sub"
        nested.mkdir()
        (nested / "inner.svg").write_text("<svg/>", encoding="utf-8")
        skipped = tmp_path / "skip"
        skipped.mkdir()
        (skipped / "hidden.svg").write_text("<svg/>", encoding="utf-8")
        (tmp_path / ".svgguardignore").write_text(
            "# generated\nskip/\n", encoding="utf-8"
        )

        flat = [key for _, key in collect_svgs([tmp_path])]
        assert flat == ["top.svg"]

        deep = [key for _, key in collect_svgs([tmp_path], recursive=True)]
        assert "top.svg" in deep
        assert "sub/inner.svg" in deep
        assert all("hidden.svg" not in key for key in deep)


class TestBrowserRunnerContract:
    """Structural checks on BrowserRunner that need no real browser.

    The full launch/teardown path is exercised end-to-end by every
    check_directory call (it builds a BrowserRunner internally); here we
    only lock the public surface so library users can rely on it.
    """

    def test_default_config_when_none(self):
        from svg_guard.checker import BrowserRunner, DetectionConfig

        r = BrowserRunner()
        assert isinstance(r.config, DetectionConfig)
        # Defaults are the shipped ones.
        assert r.config.viewport_w == 1600 and r.config.viewport_h == 1200

    def test_uses_provided_config(self):
        from svg_guard.checker import BrowserRunner, DetectionConfig

        cfg = DetectionConfig(viewport_w=800, viewport_h=600)
        r = BrowserRunner(cfg)
        assert r.config.viewport_w == 800 and r.config.viewport_h == 600

    def test_page_is_none_before_enter(self):
        from svg_guard.checker import BrowserRunner

        r = BrowserRunner()
        assert r.page is None


class TestCheckDirectoryViaCli:
    """Playwright-driven behaviour, exercised through ``python -m svg_guard``."""

    def _run(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "svg_guard", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def _seed(self, tmp_path: Path, name: str) -> Path:
        dst = tmp_path / name
        dst.write_text(
            FIXTURES.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8"
        )
        return dst

    def test_accepts_path_or_str(self, tmp_path):
        # check_directory accepts str | Path; verified via the CLI (which passes
        # the --dir string straight through).
        self._seed(tmp_path, "good.svg")
        r = self._run("check", "--dir", str(tmp_path), cwd=tmp_path)
        assert r.returncode == 0, r.stderr

    def test_corrupt_svg_recorded_as_error_not_ok(self, tmp_path):
        # A truncated/invalid SVG must surface as a render error → exit 2,
        # NOT a silent "clean" pass (exit 0).
        self._seed(tmp_path, "good.svg")
        (tmp_path / "broken.svg").write_text("<svg><rect", encoding="utf-8")
        r = self._run("check", "--dir", str(tmp_path), cwd=tmp_path)
        assert r.returncode == 2, r.stderr
        # And the broken file is reported as an error, not a clean file.
        assert "[ERR]" in r.stdout or "broken.svg" in r.stdout

    def test_json_out_writes_serializable_report(self, tmp_path):
        self._seed(tmp_path, "good.svg")
        (tmp_path / "broken.svg").write_text("<svg><", encoding="utf-8")
        json_path = tmp_path / "out.json"
        r = self._run(
            "check", "--dir", str(tmp_path), "--json", str(json_path), cwd=tmp_path
        )
        assert r.returncode == 2, r.stderr  # render error → exit 2
        assert json_path.exists()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        # Clean files are omitted; the broken file shows up under its own key
        # with an "error" entry rather than an empty issue list.
        assert "good.svg" not in report
        assert "broken.svg" in report
        assert "error" in report["broken.svg"]
