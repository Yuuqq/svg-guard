"""Test fixtures for svg-guard."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# We intentionally do NOT override the ``page`` fixture here: pytest-playwright
# already provides one (with browser-matrix parametrization). Overriding it
# with a custom viewport caused cross-file fixture clashes when running the
# whole suite. check_directory/check_svg compute scale from the actual
# rendered size, so the default viewport (1280x720) works fine.
