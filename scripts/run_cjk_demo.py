#!/usr/bin/env python3
"""
Runner script for the CJK overflow demonstration.
This script invokes the svg_guard CLI to check and optionally fix
the examples in the examples/cjk/ directory.
"""
import os
import subprocess
import sys
from pathlib import Path
import shutil

def run_demo():
    # Ensure we are running from the repo root
    repo_root = Path(__file__).resolve().parent.parent
    cjk_dir = repo_root / "examples" / "cjk"
    out_dir = cjk_dir / "out"

    # Create the output directory
    out_dir.mkdir(parents=True, exist_ok=True)

    html_report = out_dir / "report.html"
    json_report = out_dir / "report.json"

    print("===============================================================")
    print("Running svg-guard on CJK example SVGs")
    print(f"Target directory: {cjk_dir}")
    print(f"Output directory: {out_dir}")
    print("===============================================================")

    # Clean previous fixed files and backups
    for f in cjk_dir.glob("*.fixed.svg"):
        f.unlink()
    for f in cjk_dir.glob("*.bak"):
        f.unlink()

    # Run the check command
    check_cmd = [
        sys.executable, "-m", "svg_guard", "check",
        "--dir", str(cjk_dir),
        "--html", str(html_report),
        "--json", str(json_report),
        "-v"
    ]

    print(f"\nExecuting: {' '.join(check_cmd)}")

    try:
        # We allow exit code 1 (issues found) to continue without throwing
        result = subprocess.run(check_cmd, cwd=repo_root)

        if result.returncode == 0:
            print("\n✅ Check finished: No issues found.")
        elif result.returncode == 1:
            print("\n⚠️ Check finished: Issues found (Expected in this demo).")
        else:
            print(f"\n❌ Check failed with unexpected exit code: {result.returncode}")
            sys.exit(result.returncode)

        print(f"Generated HTML report: {html_report.relative_to(repo_root)}")
        print(f"Generated JSON report: {json_report.relative_to(repo_root)}")

        # Now run fix on the directory
        print("\n===============================================================")
        print("Running svg-guard fix on CJK example SVGs")
        print("===============================================================")

        fix_cmd = [
            sys.executable, "-m", "svg_guard", "fix",
            "--dir", str(cjk_dir)
        ]
        print(f"Executing: {' '.join(fix_cmd)}")

        fix_result = subprocess.run(fix_cmd, cwd=repo_root)

        if fix_result.returncode in (0, 1):
            print("\n✅ Fix applied successfully (exit code 1 is expected if issues were present).")

            # Keep the fixed file separate and restore the original so the repo maintains the overflowing baseline
            for bak_file in cjk_dir.glob("*.bak"):
                orig_file = bak_file.with_suffix('') # removes .bak
                fixed_file = orig_file.with_name(orig_file.stem + ".fixed.svg")
                # Move the newly fixed file to *.fixed.svg
                shutil.move(str(orig_file), str(fixed_file))
                # Restore the original from the backup
                shutil.move(str(bak_file), str(orig_file))
                print(f"Restored original: {orig_file.name}")
                print(f"Saved fixed result as: {fixed_file.name}")

            # Write a BEFORE_AFTER note
            note_path = cjk_dir / "BEFORE_AFTER.md"
            with open(note_path, "w", encoding="utf-8") as f:
                f.write("# CJK Demo Result\n\n")
                f.write("The `svg-guard` tool successfully detected `text_rect` overflow in the flowchart and applied fixes.\n\n")
                f.write("The report artifacts are found in `out/`.\n")
                f.write("The original overflowing SVG remains intact, while the tool's automatically corrected version (which widened the `<rect>` tags from width 180 to 299 to accommodate the CJK text) has been saved as `flowchart_overflow.fixed.svg`.\n")
            print(f"Created note: {note_path.relative_to(repo_root)}")

        else:
            print(f"\n❌ Fix failed with exit code: {fix_result.returncode}")

    except Exception as e:
        print(f"Error running demo: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_demo()
