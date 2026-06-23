"""CLI entry point for svg-guard.

Exit code contract (shared by all subcommands):

  * ``0`` — success, no overflow issues (and no render errors)
  * ``1`` — overflow issues found (check), or fixes still needed (fix)
  * ``2`` — tool-level error: missing directory, Playwright not installed,
            or one or more SVGs failed to render and could not be checked
"""

from __future__ import annotations

import argparse
import logging
import sys


def _has_render_errors(results: dict) -> bool:
    """True if any CheckResult carries a render error."""
    return any(getattr(r, "error", None) is not None for r in results.values())


def _configure_logging(verbose: bool) -> None:
    """Attach a handler to the svg_guard logger so the CLI reproduces the
    progress output that used to come from bare ``print`` calls.

    Library callers don't get this handler, so importing svg_guard is silent
    by default. Output goes to stdout (matching the old print target) with no
    level prefix so existing CI log scrapers keep working.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("svg_guard")
    logger.setLevel(level)
    # Replace any prior handler so repeated main() calls in tests don't stack.
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.addHandler(handler)
    logger.propagate = False


def main(argv: list[str] | None = None) -> None:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="svg-guard",
        description="Detect and fix text overflow in SVG diagrams.",
    )
    parser.add_argument(
        "--version", action="version", version=f"svg-guard {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── check ──────────────────────────────────────────────
    p_check = sub.add_parser("check", help="Detect overflow issues")
    p_check.add_argument("--dir", default=".", help="Directory containing SVG files")
    p_check.add_argument("--verbose", "-v", action="store_true")
    p_check.add_argument("--json", dest="json_out", help="Write JSON report")
    p_check.add_argument("--html", dest="html_out", help="Write HTML visual report")

    # ── fix ────────────────────────────────────────────────
    p_fix = sub.add_parser("fix", help="Auto-fix overflow issues")
    p_fix.add_argument("--dir", default=".", help="Directory containing SVG files")
    p_fix.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing"
    )
    p_fix.add_argument(
        "--no-backup", action="store_true", help="Skip creating .bak files"
    )

    # ── report ─────────────────────────────────────────────
    p_report = sub.add_parser("report", help="Generate HTML report only")
    p_report.add_argument("--dir", default=".", help="Directory containing SVG files")
    p_report.add_argument(
        "--output", "-o", default="svg-guard-report.html", help="Output HTML path"
    )

    args = parser.parse_args(argv)

    _configure_logging(args.verbose if args.command == "check" else False)

    try:
        if args.command == "check":
            _cmd_check(args)
        elif args.command == "fix":
            _cmd_fix(args)
        elif args.command == "report":
            _cmd_report(args)
    except FileNotFoundError as e:
        # Library callers still get the exception; CLI users get a clean message.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # Catch-all: never leak a raw traceback to the CLI user. Covers
        # "Playwright not installed", browser launch failure, etc.
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)


def _cmd_check(args: argparse.Namespace) -> None:
    from .checker import check_directory
    from .report import generate_report

    results, total_issues = check_directory(
        args.dir, verbose=args.verbose, json_out=args.json_out
    )

    if args.html_out:
        generate_report(results, args.html_out)
        print(f"HTML report saved to {args.html_out}")

    # Render errors take precedence: a file we couldn't check must not be
    # reported as a clean pass, even if no other issues were found.
    if _has_render_errors(results):
        sys.exit(2)
    sys.exit(1 if total_issues > 0 else 0)


def _cmd_fix(args: argparse.Namespace) -> None:
    from .checker import check_directory
    from .fixer import fix_svg

    results, total_issues = check_directory(args.dir)
    had_render_errors = _has_render_errors(results)

    total_fixes = 0
    for name, result in results.items():
        if result.ok:
            continue
        if result.error is not None:
            # Can't fix a file we failed to measure; surface it but skip writing.
            print(f"  [skip] {name}: render error, not fixable ({result.error})")
            continue
        changes = fix_svg(
            result.path,
            result.issues,
            backup=not args.no_backup,
            dry_run=args.dry_run,
        )
        if changes:
            total_fixes += len(changes)
            mode = "would fix" if args.dry_run else "fixed"
            for change in changes:
                print(f"  [{mode}] {name}: {change}")

    if total_fixes == 0:
        print("No fixable issues found.")
    else:
        action = "Would apply" if args.dry_run else "Applied"
        print(f"\n{action} {total_fixes} fixes.")

    if args.dry_run and total_fixes > 0:
        print("Run without --dry-run to apply changes.")

    # Exit 2 if any file failed to render (couldn't be processed at all);
    # otherwise 1 if there were issues to fix, else 0.
    if had_render_errors:
        sys.exit(2)
    sys.exit(1 if total_issues > 0 else 0)


def _cmd_report(args: argparse.Namespace) -> None:
    from .checker import check_directory
    from .report import generate_report

    results, _ = check_directory(args.dir)
    path = generate_report(results, args.output)
    print(f"Report saved to {path}")
    # Mirror the other subcommands: always exit through sys.exit so the exit
    # code contract is uniform whether main() runs as a process or is invoked
    # in-process by tests.
    if _has_render_errors(results):
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
