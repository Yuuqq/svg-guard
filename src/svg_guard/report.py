"""HTML and JSON report generators for svg-guard overflow results."""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime
from pathlib import Path

from .checker import CheckResult, Issue
from ._io import read_svg, write_text_atomic

# Cap on how many bytes of source SVG we inline into the report as a preview
# image. Huge SVGs would bloat the HTML and slow browsers; above this we fall
# back to a coordinate-only schematic.
_MAX_PREVIEW_BYTES = 200_000

# viewBox="min-x min-y width height" — used to align the overlay to the SVG's
# own coordinate system when the source can't be inlined.
_VIEWBOX_RE = re.compile(
    r"""viewBox\s*=\s*["']\s*([+-]?\d*\.?\d+)\s+([+-]?\d*\.?\d+)\s+([+-]?\d*\.?\d+)\s+([+-]?\d*\.?\d+)\s*["']""",
    re.IGNORECASE,
)

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
.issue-row { display: grid; grid-template-columns: 28px 80px 100px 1fr;
             gap: 12px; padding: 10px 0; border-top: 1px solid #f1f5f9;
             font-size: 14px; align-items: baseline; cursor: default; }
.issue-row.highlighted { background: #fef2f2; }
.issue-idx { font-weight: 700; color: #dc2626; text-align: center; }
.issue-type { font-weight: 700; color: #6366f1; }
.issue-dir { font-weight: 600; }
.issue-dir.text-overflow { color: #dc2626; }
.issue-dir.viewbox-overflow { color: #d97706; }
.issue-text { color: #475569; word-break: break-all; }
.badge { display: inline-block; background: #fef2f2; color: #dc2626;
         border-radius: 6px; padding: 2px 8px; font-size: 13px; font-weight: 700; }
.empty { text-align: center; color: #16a34a; font-size: 18px;
         font-weight: 700; padding: 48px 0; }

/* Visual preview: original SVG image with a red-box overlay. */
.preview { position: relative; margin-bottom: 16px; border: 1px solid #e2e8f0;
           border-radius: 8px; overflow: hidden; background:
           repeating-conic-gradient(#f1f5f9 0% 25%, #fff 0% 50%) 50% / 20px 20px; }
.preview svg.preview-img { display: block; width: 100%; height: auto; }
.preview .overlay { position: absolute; inset: 0; width: 100%; height: 100%;
                    pointer-events: none; }
.overlay rect.box { fill: rgba(220, 38, 38, 0.12); stroke: #dc2626;
                    stroke-width: 2; vector-effect: non-scaling-stroke; }
.overlay rect.box.active { fill: rgba(220, 38, 38, 0.28); }
.overlay rect.parent { fill: rgba(99, 102, 241, 0.06); stroke: #6366f1;
                        stroke-width: 1.5; stroke-dasharray: 4 3;
                        vector-effect: non-scaling-stroke; }
.overlay .num { fill: #fff; stroke: #dc2626; stroke-width: 2;
                paint-order: stroke; font: bold 11px sans-serif; }
.preview-note { font-size: 12px; color: #94a3b8; margin-top: 6px; }
.preview .legend { display: inline-flex; gap: 12px; font-size: 11px;
                   color: #64748b; padding: 6px 10px; }
.legend .sw { display: inline-block; width: 10px; height: 10px;
              vertical-align: middle; margin-right: 4px; border-radius: 2px; }
.legend .sw.over { background: rgba(220,38,38,0.4); border: 1px solid #dc2626; }
.legend .sw.par { background: rgba(99,102,241,0.15); border: 1px dashed #6366f1; }
"""

# Small script that links an issue row to its overlay box: hovering either
# end highlights the other. Kept tiny and dependency-free.
_JS = """
<script>
(function () {
  document.querySelectorAll('.issue-row[data-idx]').forEach(function (row) {
    var idx = row.getAttribute('data-idx');
    var box = document.querySelector('.overlay rect.box[data-idx="' + idx + '"]');
    if (!box) return;
    function on() { row.classList.add('highlighted'); box.classList.add('active'); }
    function off() { row.classList.remove('highlighted'); box.classList.remove('active'); }
    row.addEventListener('mouseenter', on);
    row.addEventListener('mouseleave', off);
  });
})();
</script>
"""


def _viewbox_dims(result: CheckResult) -> tuple[float, float] | None:
    """Best-effort (w, h) of the SVG's viewBox for overlay alignment.

    Prefers the measured viewBox the checker stored on the result; falls
    back to parsing the source file's viewBox attribute.
    """
    vb = result.viewBox
    if isinstance(vb, dict) and vb.get("w") and vb.get("h"):
        return float(vb["w"]), float(vb["h"])
    try:
        src = read_svg(result.path)
    except OSError:
        return None
    m = _VIEWBOX_RE.search(src)
    if m:
        return float(m.group(3)), float(m.group(4))
    return None


def _issue_box(issue: Issue) -> tuple[float, float, float, float] | None:
    """The (x, y, w, h) the overlay should outline for an issue."""
    svg = issue.svg or {}
    try:
        return float(svg["x"]), float(svg["y"]), float(svg["w"]), float(svg["h"])
    except (KeyError, TypeError, ValueError):
        return None


def _parent_box(issue: Issue) -> tuple[float, float, float, float] | None:
    """The parent rect's (x, y, w, h), when present (text_rect issues)."""
    parent = issue.parent or {}
    svg = parent.get("svg") if isinstance(parent, dict) else None
    if not isinstance(svg, dict):
        return None
    try:
        return float(svg["x"]), float(svg["y"]), float(svg["w"]), float(svg["h"])
    except (KeyError, TypeError, ValueError):
        return None


def _render_preview(result: CheckResult) -> str:
    """Build the preview HTML for one file: original SVG + red-box overlay.

    Returns '' when no preview can be produced (no viewBox, or all issues
    lack coordinates) — the caller just renders the issue list instead.
    """
    vb = _viewbox_dims(result)
    if vb is None:
        return ""
    vbw, vbh = vb

    # Try to inline the real SVG as an <img> (data URI) so the preview shows
    # the actual rendering. Skip if the file is missing or too large.
    img_tag = ""
    try:
        src = read_svg(result.path)
    except OSError:
        src = ""
    if src and len(src.encode("utf-8")) <= _MAX_PREVIEW_BYTES:
        b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
        img_tag = (
            f'<img class="preview-img" alt="{html.escape(result.path.name)}" '
            f'src="data:image/svg+xml;base64,{b64}">'
        )

    # Build the overlay: one red box per issue with a coordinate, plus a
    # dashed parent-rect box for text_rect issues. viewBox matches the SVG so
    # coordinates line up 1:1. vector-effect keeps strokes crisp at any scale.
    overlay_shapes: list[str] = []
    legend = ""
    has_parent = False
    for idx, issue in enumerate(result.issues, start=1):
        box = _issue_box(issue)
        if box is None:
            continue
        x, y, w, h = box
        # Number label sits at the box's top-left corner.
        overlay_shapes.append(
            f'<rect class="box" data-idx="{idx}" '
            f'x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}">'
            f"<title>#{idx} {html.escape(issue.type)} {html.escape(issue.direction)}</title></rect>"
        )
        overlay_shapes.append(
            f'<text class="num" x="{x + 4:.0f}" y="{y + 12:.0f}">{idx}</text>'
        )
        pbox = _parent_box(issue)
        if pbox:
            has_parent = True
            px, py, pw, ph = pbox
            overlay_shapes.append(
                f'<rect class="parent" x="{px:.0f}" y="{py:.0f}" '
                f'width="{pw:.0f}" height="{ph:.0f}">'
                f"<title>parent rect #{idx}</title></rect>"
            )

    if not overlay_shapes:
        return ""  # nothing drawable

    if has_parent:
        legend = (
            '<div class="legend">'
            '<span><i class="sw over"></i>overflow</span>'
            '<span><i class="sw par"></i>parent rect</span>'
            "</div>"
        )

    overlay = (
        f'<svg class="overlay" viewBox="0 0 {vbw:.0f} {vbh:.0f}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg">' + "".join(overlay_shapes) + "</svg>"
    )

    note = (
        ""
        if img_tag
        else (
            '<div class="preview-note">schematic only '
            "(source too large to inline; coordinates from measurement)</div>"
        )
    )

    return (
        '<div class="preview">' + legend + (img_tag or "") + overlay + "</div>" + note
    )


def generate_report(
    results: dict[str, CheckResult],
    output_path: Path | str,
) -> Path:
    """Generate a self-contained HTML report."""
    output_path = Path(output_path)
    total_files = len(results)
    # Distinguish genuine overflow issues from render errors — a file that
    # failed to render is "not ok" but has zero issues, and should be reported
    # as an error rather than lumped into the issues count.
    files_with_issues = sum(1 for r in results.values() if not r.ok and r.error is None)
    files_with_errors = sum(1 for r in results.values() if r.error is not None)
    total_issues = sum(len(r.issues) for r in results.values())

    parts = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>SVG Guard Report — {total_files} files</title>",
        f"<style>{_CSS}</style>",
        _JS,
        "</head><body>",
        "<header>",
        "<h1>SVG Guard Report</h1>",
        f'<p class="meta">{datetime.now().strftime("%Y-%m-%d %H:%M")} — '
        f"{total_files} files checked</p>",
        '<div class="stats">',
        f'<div class="stat"><div class="stat-val">{total_files}</div>'
        f'<div class="stat-label">Files</div></div>',
        f'<div class="stat"><div class="stat-val {"ok" if files_with_issues == 0 else "bad"}">'
        f'{files_with_issues}</div><div class="stat-label">With Issues</div></div>',
        f'<div class="stat"><div class="stat-val {"ok" if files_with_errors == 0 else "bad"}">'
        f'{files_with_errors}</div><div class="stat-label">Errors</div></div>',
        f'<div class="stat"><div class="stat-val {"ok" if total_issues == 0 else "bad"}">'
        f'{total_issues}</div><div class="stat-label">Issues</div></div>',
        "</div></header>",
        "<main>",
    ]

    if total_issues == 0 and files_with_errors == 0:
        parts.append(
            '<p class="empty">All SVG files passed — no overflow issues found.</p>'
        )
    else:
        for name, result in results.items():
            if result.ok:
                continue
            parts.append('<div class="file-card">')
            parts.append(f'<div class="file-name">{html.escape(name)}</div>')
            if result.error is not None:
                # Render error: show what went wrong instead of fake "0 issues".
                parts.append(
                    '<div class="file-issues">'
                    '<span class="badge">render error</span></div>'
                )
                parts.append(
                    f'<div class="issue-row">'
                    f'<span class="issue-idx"></span>'
                    f'<span class="issue-type">error</span>'
                    f'<span class="issue-dir viewbox-overflow">render failed</span>'
                    f'<span class="issue-text">{html.escape(result.error)}</span>'
                    f"</div>"
                )
            else:
                parts.append(
                    f'<div class="file-issues">'
                    f'<span class="badge">{len(result.issues)} issues</span></div>'
                )
                # Visual preview (original SVG + red-box overlay), if drawable.
                parts.append(_render_preview(result))
                for idx, issue in enumerate(result.issues, start=1):
                    dir_class = (
                        "text-overflow"
                        if "viewbox" not in issue.direction
                        else "viewbox-overflow"
                    )
                    parts.append(
                        f'<div class="issue-row" data-idx="{idx}">'
                        f'<span class="issue-idx">{idx}</span>'
                        f'<span class="issue-type">{html.escape(issue.type)}</span>'
                        f'<span class="issue-dir {dir_class}">{html.escape(issue.direction)}</span>'
                        f'<span class="issue-text">{html.escape(issue.text)}</span>'
                        f"</div>"
                    )
            parts.append("</div>")

    parts.append("</main></body></html>")

    write_text_atomic(output_path, "\n".join(parts))
    return output_path


def write_json_report(results: dict[str, CheckResult], output_path: Path | str) -> Path:
    """Serialize check results to a JSON file (machine-readable, for CI).

    Schema (one entry per file with problems):
      { "file.svg": [ {issue...}, ... ],     # files with overflow issues
        "broken.svg": { "error": "..." } }   # files that failed to render

    Clean files are omitted. Writing is atomic (see _io.write_text_atomic).
    """
    output_path = Path(output_path)
    report: dict[str, object] = {}
    for name, result in results.items():
        if result.error is not None:
            # Surface errored files explicitly so CI tooling can see them,
            # distinct from "no issues".
            report[name] = {"error": result.error}
        elif not result.ok:
            report[name] = [
                {
                    "type": i.type,
                    "text": i.text,
                    "direction": i.direction,
                    "svg": i.svg,
                    "parent": i.parent,
                    "fix": i.fix,
                }
                for i in result.issues
            ]
    write_text_atomic(output_path, json.dumps(report, indent=2, ensure_ascii=False))
    return output_path
