"""CLI entry point for svg-guard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="svg-guard",
        description="Detect and fix text overflow in SVG diagrams.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── check ──────────────────────────────────────────────
    p_check = sub.add_parser("check", help="Detect overflow issues")
    p_check.add_argument("--dir", default=".", help="Directory containing SVG files")
    p_check.add_argument("--verbose", "-v", action="store_true")
    p_check.add_argument("--json", dest="json_out", help="Write JSON report")
    p_check.add_argument(
        "--html", dest="html_out", help="Write HTML visual report"
    )

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

    if args.command == "check":
        _cmd_check(args)
    elif args.command == "fix":
        _cmd_fix(args)
    elif args.command == "report":
        _cmd_report(args)


def _cmd_check(args: argparse.Namespace) -> None:
    from .checker import check_directory
    from .report import generate_report

    results, total_issues = check_directory(
        args.dir, verbose=args.verbose, json_out=args.json_out
    )

    if args.html_out:
        generate_report(results, args.html_out)
        print(f"HTML report saved to {args.html_out}")

    sys.exit(1 if total_issues > 0 else 0)


def _cmd_fix(args: argparse.Namespace) -> None:
    from .checker import check_directory
    from .fixer import fix_svg

    results, _ = check_directory(args.dir)

    total_fixes = 0
    for name, result in results.items():
        if result.ok:
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


def _cmd_report(args: argparse.Namespace) -> None:
    from .checker import check_directory
    from .report import generate_report

    results, _ = check_directory(args.dir)
    path = generate_report(results, args.output)
    print(f"Report saved to {path}")


if __name__ == "__main__":
    main()
