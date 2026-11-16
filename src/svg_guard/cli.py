"""CLI entry point for svg-guard.

Exit code contract (shared by all subcommands):

  * ``0`` — success, no overflow issues left (and no render errors)
  * ``1`` — overflow issues found (check, report), or still present after fix
  * ``2`` — tool-level error: missing path, Playwright not installed,
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


def _add_scan_args(parser: argparse.ArgumentParser) -> None:
    """Files or directories a subcommand should measure.

    ``svg-guard check diagram.svg`` and ``svg-guard check --dir diagrams -r``
    both work. With no path, the current directory is scanned.
    """
    parser.add_argument(
        "paths",
        nargs="*",
        help="SVG files or directories (default: the current directory)",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Directory of SVG files, added to any paths given",
    )
    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="Scan subdirectories. A .svgguardignore in each directory skips matches",
    )


def _scan_paths(args: argparse.Namespace) -> list[str]:
    paths = [str(p) for p in (args.paths or [])]
    if getattr(args, "dir", None):
        paths.append(args.dir)
    if not paths:
        paths.append(".")
    return paths


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
    _add_scan_args(p_check)
    p_check.add_argument("--verbose", "-v", action="store_true")
    p_check.add_argument("--json", dest="json_out", help="Write JSON report")
    p_check.add_argument("--html", dest="html_out", help="Write HTML visual report")

    # ── fix ────────────────────────────────────────────────
    p_fix = sub.add_parser("fix", help="Auto-fix overflow issues")
    _add_scan_args(p_fix)
    p_fix.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing"
    )
    p_fix.add_argument(
        "--no-backup", action="store_true", help="Skip creating .bak files"
    )

    # ── report ─────────────────────────────────────────────
    p_report = sub.add_parser("report", help="Generate HTML report only")
    _add_scan_args(p_report)
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
    from .checker import check_paths
    from .report import generate_report

    results, total_issues = check_paths(
        _scan_paths(args),
        recursive=args.recursive,
        verbose=args.verbose,
        json_out=args.json_out,
    )

    if args.html_out:
        generate_report(results, args.html_out)
        print(f"HTML report saved to {args.html_out}")

    # Render errors take precedence: a file we couldn't check must not be
    # reported as a clean pass, even if no other issues were found.
    if _has_render_errors(results):
        sys.exit(2)
    sys.exit(1 if total_issues > 0 else 0)


_MAX_FIX_PASSES = 3


def _cmd_fix(args: argparse.Namespace) -> None:
    from .checker import BrowserRunner, check_paths, check_svg, collect_svgs
    from .fixer import fix_svg

    targets = _scan_paths(args)
    # Resolve paths before Chromium starts, so a missing directory is a fast
    # exit 2 instead of a browser launch followed by the same error.
    collect_svgs(targets, recursive=args.recursive)

    # Separate real modifications from skip notices. fix_svg() reports both in
    # one list of strings; a skip notice always contains the word "skipped".
    def _is_skip(msg: str) -> bool:
        return "skipped" in msg

    total_applied = 0
    total_skipped = 0
    mode = "would fix" if args.dry_run else "fixed"

    def _apply(results: dict, only: set[str] | None) -> list[str]:
        """Fix files that still have issues. Returns keys that were written.

        ``only`` limits a later pass to files the previous pass actually
        changed, so a render error or an unfixable label is not reported twice.
        """
        nonlocal total_applied, total_skipped
        written: list[str] = []
        for name, result in results.items():
            if only is not None and name not in only:
                continue
            if result.ok:
                continue
            if result.error is not None:
                if only is None:
                    print(
                        f"  [skip] {name}: render error, not fixable ({result.error})"
                    )
                    total_skipped += 1
                continue
            changes = fix_svg(
                result.path,
                result.issues,
                backup=not args.no_backup,
                dry_run=args.dry_run,
            )
            applied = [c for c in changes if not _is_skip(c)]
            skipped = [c for c in changes if _is_skip(c)]
            total_applied += len(applied)
            if only is None:
                total_skipped += len(skipped)
            for change in applied:
                print(f"  [{mode}] {name}: {change}")
            if only is None:
                for change in skipped:
                    print(f"  [skip] {name}: {change}")
            if applied and not args.dry_run:
                written.append(name)
        return written

    with BrowserRunner() as runner:
        results, _ = check_paths(targets, recursive=args.recursive, runner=runner)
        # Re-measure after each write. Widening a card can push it outside
        # the viewBox, and one pass of rounding can leave a sliver of
        # overflow. Stop early when a pass changes nothing, or when the
        # files we just wrote come back clean. Dry-run never writes, so it
        # does a single pass and keeps the original exit code.
        pending: set[str] | None = None
        for _ in range(_MAX_FIX_PASSES):
            written = _apply(results, pending)
            if args.dry_run or not written:
                break
            for name in written:
                results[name] = check_svg(
                    runner.page, results[name].path, config=runner.config
                )
            if all(results[name].ok for name in written):
                break
            pending = set(written)

    if total_applied == 0 and total_skipped == 0:
        print("No fixable issues found.")
    else:
        action = "Would apply" if args.dry_run else "Applied"
        summary = f"\n{action} {total_applied} fix(es)"
        if total_skipped:
            summary += (
                f"; {total_skipped} issue(s) skipped as not auto-fixable "
                f"(manual edit needed)"
            )
        print(summary + ".")

    if args.dry_run and total_applied > 0:
        print("Run without --dry-run to apply changes.")

    # Exit 2 if any file failed to render. Otherwise 1 when something is
    # still wrong after fixing (or would still be wrong, on a dry run),
    # and 0 when the files are clean.
    if _has_render_errors(results):
        sys.exit(2)
    still_bad = any(not r.ok and r.error is None for r in results.values())
    sys.exit(1 if still_bad else 0)


def _cmd_report(args: argparse.Namespace) -> None:
    from .checker import check_paths
    from .report import generate_report

    results, total_issues = check_paths(_scan_paths(args), recursive=args.recursive)
    path = generate_report(results, args.output)
    print(f"Report saved to {path}")
    # Same gate as check: a report of a broken tree must not exit 0, or CI
    # that only runs `report` will go green on overflow.
    if _has_render_errors(results):
        sys.exit(2)
    sys.exit(1 if total_issues > 0 else 0)


if __name__ == "__main__":
    main()
