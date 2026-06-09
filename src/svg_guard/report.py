"""HTML report generator — annotated visual report of SVG overflow issues."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .checker import CheckResult

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
       background: #f8fafc; color: #1e293b; padding: 24px; }
header { max-width: 900px; margin: 0 auto 32px; }
h1 { font-size: 28px; font-weight: 800; margin-bottom: 8px; }
.meta { color: #64748b; font-size: 15px; }
.stats { display: flex; gap: 16px; margin-top: 16px; }
.stat { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
        padding: 16px 24px; text-align: center; min-width: 120px; }
.stat-val { font-size: 28px; font-weight: 800; }
.stat-label { font-size: 13px; color: #64748b; margin-top: 4px; }
.stat-val.ok { color: #16a34a; }
.stat-val.bad { color: #dc2626; }
main { max-width: 900px; margin: 0 auto; }
.file-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 16px;
             padding: 24px; margin-bottom: 16px; }
.file-name { font-size: 18px; font-weight: 700; margin-bottom: 4px;
             font-family: "SF Mono", "Cascadia Code", monospace; }
.file-issues { color: #dc2626; font-size: 14px; margin-bottom: 16px; }
.issue-row { display: grid; grid-template-columns: 80px 100px 1fr;
             gap: 12px; padding: 10px 0; border-top: 1px solid #f1f5f9;
             font-size: 14px; align-items: baseline; }
.issue-type { font-weight: 700; color: #6366f1; }
.issue-dir { font-weight: 600; }
.issue-dir.text-overflow { color: #dc2626; }
.issue-dir.viewbox-overflow { color: #d97706; }
.issue-text { color: #475569; word-break: break-all; }
.badge { display: inline-block; background: #fef2f2; color: #dc2626;
         border-radius: 6px; padding: 2px 8px; font-size: 13px; font-weight: 700; }
.empty { text-align: center; color: #16a34a; font-size: 18px;
         font-weight: 700; padding: 48px 0; }
"""


def generate_report(
    results: dict[str, CheckResult],
    output_path: Path | str,
) -> Path:
    """Generate a self-contained HTML report."""
    output_path = Path(output_path)
    total_files = len(results)
    files_with_issues = sum(1 for r in results.values() if not r.ok)
    total_issues = sum(len(r.issues) for r in results.values())

    parts = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>SVG Guard Report — {total_files} files</title>",
        f"<style>{_CSS}</style></head><body>",
        "<header>",
        "<h1>SVG Guard Report</h1>",
        f'<p class="meta">{datetime.now().strftime("%Y-%m-%d %H:%M")} — '
        f"{total_files} files checked</p>",
        '<div class="stats">',
        f'<div class="stat"><div class="stat-val">{total_files}</div>'
        f'<div class="stat-label">Files</div></div>',
        f'<div class="stat"><div class="stat-val {"ok" if files_with_issues == 0 else "bad"}">'
        f"{files_with_issues}</div><div class=\"stat-label\">With Issues</div></div>",
        f'<div class="stat"><div class="stat-val {"ok" if total_issues == 0 else "bad"}">'
        f"{total_issues}</div><div class=\"stat-label\">Issues</div></div>",
        "</div></header>",
        "<main>",
    ]

    if total_issues == 0:
        parts.append('<p class="empty">All SVG files passed — no overflow issues found.</p>')
    else:
        for name, result in results.items():
            if result.ok:
                continue
            parts.append(f'<div class="file-card">')
            parts.append(f'<div class="file-name">{html.escape(name)}</div>')
            parts.append(
                f'<div class="file-issues">'
                f'<span class="badge">{len(result.issues)} issues</span></div>'
            )
            for issue in result.issues:
                dir_class = (
                    "text-overflow" if "viewbox" not in issue.direction
                    else "viewbox-overflow"
                )
                parts.append(
                    f'<div class="issue-row">'
                    f'<span class="issue-type">{html.escape(issue.type)}</span>'
                    f'<span class="issue-dir {dir_class}">{html.escape(issue.direction)}</span>'
                    f'<span class="issue-text">{html.escape(issue.text)}</span>'
                    f"</div>"
                )
            parts.append("</div>")

    parts.append("</main></body></html>")

    output_path.write_text("\n".join(parts), encoding="utf-8")
    return output_path
