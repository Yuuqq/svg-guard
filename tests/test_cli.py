"""Tests for the CLI surface that need no browser.

These cover the parts of ``cli.py`` that are pure argument/exit-code logic
and don't require Playwright: the version flag, unknown subcommands, the
missing-directory error path, and the exit-code contract on FileNotFoundError.
The browser-dependent happy paths are exercised by the Playwright suites
(test_checker/test_fixer).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from svg_guard import cli as cli_mod
from svg_guard import __version__


def _run(argv: list[str]):
    """Invoke ``main`` and capture the SystemExit code."""
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(argv)
    return excinfo.value.code


class TestVersionAndUsage:
    def test_version_flag_prints_version_and_exits_zero(self, capsys):
        code = _run(["--version"])
        assert code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_missing_subcommand_exits_nonzero(self):
        # argparse uses exit code 2 for usage errors.
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main([])
        assert excinfo.value.code != 0

    def test_unknown_subcommand_exits_nonzero(self):
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(["frobnicate"])
        assert excinfo.value.code != 0


class TestMissingDirectory:
    def test_check_missing_dir_exits_2(self, tmp_path, capsys):
        # Exit 2 is the documented "tool-level error" code (A3).
        missing = tmp_path / "does-not-exist"
        code = _run(["check", "--dir", str(missing)])
        assert code == 2
        err = capsys.readouterr().err
        assert "error" in err

    def test_fix_missing_dir_exits_2(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        code = _run(["fix", "--dir", str(missing)])
        assert code == 2

    def test_report_missing_dir_exits_2(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        code = _run(["report", "--dir", str(missing)])
        assert code == 2

    def test_empty_dir_exits_2(self, tmp_path, capsys):
        # Directory exists but has no .svg files → FileNotFoundError → exit 2.
        code = _run(["check", "--dir", str(tmp_path)])
        assert code == 2


class TestModuleEntryPoint:
    def test_python_dash_m_runs_cli(self):
        # ``python -m svg_guard --version`` must work via __main__.py.
        out = subprocess.run(
            [sys.executable, "-m", "svg_guard", "--version"],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0
        assert __version__ in out.stdout


class TestExitCodeContract:
    """End-to-end exit codes via ``python -m svg_guard`` in a subprocess.

    We deliberately run the CLI in a SUBPROCESS rather than calling main()
    in-process: svg-guard uses sync_playwright, and pytest-playwright (loaded
    by the browser-driven tests in this suite) keeps an asyncio loop alive
    that conflicts with a second sync_playwright instance in the same
    process. A subprocess gives a clean Playwright environment and also tests
    the real entry point users hit.
    """

    def _run_cli(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "svg_guard", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def _copy_fixture_into(self, name: str, tmp_path: Path) -> Path:
        fixtures = Path(__file__).parent / "fixtures"
        dst = tmp_path / name
        dst.write_text(
            fixtures.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8"
        )
        return dst

    def test_clean_directory_exits_0(self, tmp_path):
        self._copy_fixture_into("good.svg", tmp_path)
        r = self._run_cli("check", "--dir", str(tmp_path))
        assert r.returncode == 0, r.stderr

    def test_overflow_directory_exits_1(self, tmp_path):
        self._copy_fixture_into("text_overflow.svg", tmp_path)
        r = self._run_cli("check", "--dir", str(tmp_path))
        assert r.returncode == 1, r.stderr

    def test_directory_with_render_error_exits_2(self, tmp_path):
        self._copy_fixture_into("good.svg", tmp_path)
        # A truncated/unparseable SVG must trigger a render error → exit 2,
        # NOT a silent "clean" pass.
        (tmp_path / "broken.svg").write_text("<svg><rect", encoding="utf-8")
        r = self._run_cli("check", "--dir", str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_fix_dry_run_does_not_modify_file(self, tmp_path):
        bad = self._copy_fixture_into("text_overflow.svg", tmp_path)
        before = bad.read_text(encoding="utf-8")
        # dry-run still exits non-zero (there ARE issues to fix).
        r = self._run_cli("fix", "--dir", str(tmp_path), "--dry-run")
        assert r.returncode == 1, r.stderr
        # But the file is untouched.
        assert bad.read_text(encoding="utf-8") == before

    def test_report_writes_html(self, tmp_path):
        self._copy_fixture_into("good.svg", tmp_path)
        out_html = tmp_path / "report.html"
        r = self._run_cli("report", "--dir", str(tmp_path), "--output", str(out_html))
        assert r.returncode == 0, r.stderr
        assert out_html.exists()
        assert "SVG Guard Report" in out_html.read_text(encoding="utf-8")
