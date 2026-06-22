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
