"""HTML and JSON report generators for svg-guard overflow results."""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime, timezone
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

# Human labels + CSS classes for issue types/directions. Centralising these
# means adding a new issue type can't silently produce an unstyled column.
_TYPE_LABEL: dict[str, str] = {
    "text_rect": "text→rect",
    "rect_viewbox": "rect→viewBox",
    "text_viewbox": "text→viewBox",
    "content_misfit": "misfit",
}

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto,
       "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
       "Noto Sans SC", "Source Han Sans SC", sans-serif;
       background: #f8fafc; color: #1e293b; padding: 24px; line-height: 1.6; }
header { max-width: 960px; margin: 0 auto 32px; }
h1 { font-size: 28px; font-weight: 800; margin-bottom: 8px; }
.meta { color: #475569; font-size: 15px; }
.stats { display: flex; gap: 16px; margin-top: 16px; }
.stat { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
        padding: 16px 24px; text-align: center; min-width: 120px; }
.stat-val { font-size: 28px; font-weight: 800; line-height: 1.2; }
.stat-label { font-size: 13px; color: #475569; margin-top: 4px; }
.stat-val.ok { color: #15803d; }
.stat-val.bad { color: #b91c1c; }
main { max-width: 960px; margin: 0 auto; }
.file-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 16px;
             padding: 24px; margin-bottom: 16px; }
.file-name { font-size: 18px; font-weight: 700; margin-bottom: 4px;
             font-family: "SF Mono", "Cascadia Code", "Consolas",
             "PingFang SC", "Microsoft YaHei", monospace;
             overflow-wrap: anywhere; min-width: 0; word-break: break-word; }
.file-issues { color: #b91c1c; font-size: 14px; margin-bottom: 16px; }
.banner { border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;
          font-size: 14px; font-weight: 600; }
.banner.alert { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
.banner.ok { background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; }
.issues { list-style: none; }
.issue-row { display: grid;
             grid-template-columns: 28px minmax(92px, auto) minmax(110px, auto)
                                    minmax(96px, auto) minmax(96px, auto) 1fr;
             gap: 12px; padding: 10px 0; border-top: 1px solid #f1f5f9;
             font-size: 14px; line-height: 1.5; align-items: start; cursor: default; }
.issue-row > span { min-width: 0; }
.issue-row.highlighted { background: #fef2f2; }
.issue-row:focus-visible { outline: 2px solid #4338ca; outline-offset: 2px;
                           border-radius: 4px; }
.issue-idx { font-weight: 700; color: #b91c1c; text-align: center; }
.issue-type { font-weight: 700; color: #4338ca; }
.issue-dir { font-weight: 600; }
.issue-dir.text-overflow { color: #b91c1c; }
.issue-dir.viewbox-overflow { color: #b45309; }
.issue-coords { font-family: "SF Mono", "Cascadia Code", "Consolas", monospace;
                color: #475569; overflow-wrap: anywhere; font-size: 13px; }
.issue-fix { font-family: "SF Mono", "Cascadia Code", "Consolas", monospace;
             color: #15803d; overflow-wrap: anywhere; font-size: 13px; }
.issue-fix.unfixable { color: #b45309; }
.issue-text { color: #334155; overflow-wrap: anywhere; word-break: break-word; }
.badge { display: inline-block; background: #fee2e2; color: #991b1b;
         border-radius: 6px; padding: 2px 8px; font-size: 13px; font-weight: 700; }
.empty { text-align: center; color: #15803d; font-size: 18px;
         font-weight: 700; padding: 48px 0; }

/* Visual preview: original SVG image with a red-box overlay. */
.preview { position: relative; margin-bottom: 16px; border: 1px solid #e2e8f0;
           border-radius: 8px; overflow: hidden; background:
           repeating-conic-gradient(#f1f5f9 0% 25%, #fff 0% 50%) 50% / 20px 20px; }
.preview svg.preview-img { display: block; width: 100%; height: auto; }
.preview img.preview-img { display: block; width: 100%; height: auto; }
.preview .overlay { position: absolute; inset: 0; width: 100%; height: 100%;
                    pointer-events: none; }
.overlay rect.box { fill: rgba(220, 38, 38, 0.12); stroke: #dc2626;
                    stroke-width: 2; vector-effect: non-scaling-stroke; }
.overlay rect.box.active { fill: rgba(220, 38, 38, 0.28); }
.overlay rect.parent { fill: rgba(99, 102, 241, 0.06); stroke: #6366f1;
                        stroke-width: 1.5; stroke-dasharray: 4 3;
                        vector-effect: non-scaling-stroke; }
/* Number badge: red fill with a thick white outline so the digit reads on any
   background (paint-order draws the stroke under the fill). */
.overlay .num { fill: #dc2626; stroke: #fff; stroke-width: 3;
                paint-order: stroke; font: bold 12px sans-serif; }
.preview-note { font-size: 12px; color: #475569; margin-top: 6px; }
.preview .legend { display: inline-flex; gap: 12px; font-size: 11px;
                   color: #475569; padding: 6px 10px; }
.legend .sw { display: inline-block; width: 10px; height: 10px;
              vertical-align: middle; margin-right: 4px; border-radius: 2px; }
.legend .sw.over { background: rgba(220,38,38,0.4); border: 1px solid #dc2626; }
.legend .sw.par { background: rgba(99,102,241,0.15); border: 1px dashed #6366f1; }

@media (max-width: 640px) {
  body { padding: 12px; }
  .stats { flex-wrap: wrap; }
  .stat { flex: 1 1 calc(50% - 8px); min-width: 0; }
  /* Stack the issue grid vertically on narrow screens. */
  .issue-row { grid-template-columns: 1fr; gap: 4px; }
}

@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  .stat, .file-card { background: #1e293b; border-color: #334155; }
  .file-name { color: #f1f5f9; }
  .stat-label, .meta, .preview-note, .legend { color: #94a3b8; }
  .issue-row { border-top-color: #334155; }
  .issue-row.highlighted { background: rgba(220,38,38,0.16); }
  .issue-coords { color: #cbd5e1; }
  .issue-text { color: #cbd5e1; }
  .preview { background: repeating-conic-gradient(#1e293b 0% 25%, #0f172a 0% 50%) 50% / 20px 20px;
             border-color: #334155; }
  .banner.alert { background: rgba(220,38,38,0.18); color: #fca5a5; border-color: #7f1d1d; }
  .banner.ok { background: rgba(34,197,94,0.16); color: #86efac; border-color: #14532d; }
}

@media print {
  body { background: #fff; color: #000; padding: 0; }
  header, main { max-width: none; }
  .stat, .file-card, .preview { box-shadow: none; }
  .issue-row:focus-visible { outline: none; }
  * { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
}
"""

# Small script that links an issue row to its overlay box: hovering OR focusing
# either end highlights the other. Kept tiny and dependency-free.
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
    row.addEventListener('focus', on);
    row.addEventListener('blur', off);
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


def _coords(issue: Issue) -> str:
    """Render the measured box as a compact "(x,y WxH)" string, or ''."""
    svg = issue.svg or {}
    try:
        x, y, w, h = (
            int(float(svg["x"])),
            int(float(svg["y"])),
            int(float(svg["w"])),
            int(float(svg["h"])),
        )
    except (KeyError, TypeError, ValueError):
        return ""
    return f"({x},{y} {w}×{h})"


def _fmt_fix(fix: dict) -> tuple[str, bool]:
    """Human description of the proposed fix and whether it is auto-fixable.

    Returns (label, fixable). Recognises every fix shape the checker emits:
    text_rect (expand_w/expand_h + fixable), *_viewbox (expand_viewbox_*),
    and content_misfit (crop_*).
    """
    if not isinstance(fix, dict) or not fix:
        return "", True

    # Explicit fixable=False: either a transformed rect (width/height live in
    # pre-transform space, so the fixer can't edit them safely) or a pure
    # left/top overflow (widening grows rightward/downward, can't help).
    if fix.get("fixable") is False:
        if fix.get("transformed"):
            return "not auto-fixable (transformed rect)", False
        return "not auto-fixable (move element)", False

    # content_misfit crop.
    if "crop_w" in fix or "crop_h" in fix:
        try:
            cx, cy = int(fix.get("crop_x", 0)), int(fix.get("crop_y", 0))
            cw, ch = int(fix.get("crop_w", 0)), int(fix.get("crop_h", 0))
            return f"crop viewBox {cx},{cy} {cw}×{ch}", True
        except (TypeError, ValueError):
            return "", True

    # viewBox expansion (rect_viewbox / text_viewbox).
    dw = fix.get("expand_viewbox_w", 0)
    dh = fix.get("expand_viewbox_h", 0)
    if dw or dh:
        try:
            return f"expand viewBox +{int(dw)}w +{int(dh)}h", True
        except (TypeError, ValueError):
            return "", True

    # text_rect card expansion.
    ew = fix.get("expand_w", 0)
    eh = fix.get("expand_h", 0)
    if ew or eh:
        try:
            return f"expand rect +{int(ew)}w +{int(eh)}h", True
        except (TypeError, ValueError):
            return "", True

    return "", True


def _dir_class(direction: str) -> str:
    """CSS class for a direction string ('viewbox+...' → viewbox-overflow)."""
    return "viewbox-overflow" if "viewbox" in (direction or "") else "text-overflow"


def _stat(value: int, label: str, kind: str) -> str:
    """One stat card. ``kind`` ∈ {'ok','bad',''} controls the value color."""
    cls = f" {kind}" if kind else ""
    return (
        f'<div class="stat"><div class="stat-val{cls}">{value}</div>'
        f'<div class="stat-label">{html.escape(label)}</div></div>'
    )


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
            f'loading="lazy" '
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

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "<!DOCTYPE html>",
        '<html lang="und"><head><meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>SVG Guard Report — {total_files} files</title>",
        f"<style>{_CSS}</style>",
        _JS,
        "</head><body>",
        "<header>",
        "<h1>SVG Guard Report</h1>",
        f'<p class="meta">{when} — {total_files} files checked</p>',
        '<div class="stats">',
        _stat(total_files, "Files", ""),
        _stat(
            files_with_issues, "With Issues", "ok" if files_with_issues == 0 else "bad"
        ),
        _stat(files_with_errors, "Errors", "ok" if files_with_errors == 0 else "bad"),
        _stat(total_issues, "Issues", "ok" if total_issues == 0 else "bad"),
        "</div></header>",
        "<main>",
    ]

    # Top-level status banner: surfaces render errors loudly (previously a
    # file that failed to render was invisible unless you read each card).
    if files_with_errors > 0:
        parts.append(
            f'<div class="banner alert" role="alert">'
            f"{files_with_errors} file(s) failed to render — see below."
            "</div>"
        )

    if total_issues == 0 and files_with_errors == 0:
        parts.append(
            '<p class="empty" role="status">'
            "All SVG files passed — no overflow issues found."
            "</p>"
        )
    else:
        for name, result in results.items():
            if result.ok:
                continue
            parts.append('<div class="file-card">')
            parts.append(
                f'<div class="file-name" title="{html.escape(name)}">'
                f"{html.escape(name)}</div>"
            )
            if result.error is not None:
                # Render error: show what went wrong instead of fake "0 issues".
                parts.append(
                    '<div class="file-issues">'
                    '<span class="badge">render error</span></div>'
                )
                parts.append(
                    f'<ul class="issues"><li class="issue-row">'
                    f'<span class="issue-idx"></span>'
                    f'<span class="issue-type">error</span>'
                    f'<span class="issue-dir viewbox-overflow">render failed</span>'
                    f'<span class="issue-coords"></span>'
                    f'<span class="issue-fix"></span>'
                    f'<span class="issue-text">{html.escape(result.error)}</span>'
                    f"</li></ul>"
                )
            else:
                parts.append(
                    '<div class="file-issues">'
                    f'<span class="badge">{len(result.issues)} issues</span></div>'
                )
                # Visual preview (original SVG + red-box overlay), if drawable.
                parts.append(_render_preview(result))
                parts.append('<ul class="issues">')
                for idx, issue in enumerate(result.issues, start=1):
                    dir_class = _dir_class(issue.direction)
                    coords = _coords(issue)
                    fix_label, fixable = _fmt_fix(issue.fix)
                    type_label = _TYPE_LABEL.get(issue.type, issue.type)
                    # NOTE: keep "class=\"issue-row\" data-idx=\"N\"" as a
                    # contiguous substring — tests and the hover script key on it.
                    fix_cls = "" if fixable else " unfixable"
                    parts.append(
                        f'<li class="issue-row" data-idx="{idx}" '
                        f'data-type="{html.escape(issue.type)}" tabindex="0" '
                        f'role="button" '
                        f'aria-label="Issue {idx}: {html.escape(type_label)} '
                        f'{html.escape(issue.direction)}">'
                        f'<span class="issue-idx">{idx}</span>'
                        f'<span class="issue-type">{html.escape(type_label)}</span>'
                        f'<span class="issue-dir {dir_class}">'
                        f"{html.escape(issue.direction)}</span>"
                        f'<span class="issue-coords">{html.escape(coords)}</span>'
                        f'<span class="issue-fix{fix_cls}">{html.escape(fix_label)}</span>'
                        f'<span class="issue-text">{html.escape(issue.text)}</span>'
                        f"</li>"
                    )
                parts.append("</ul>")
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
