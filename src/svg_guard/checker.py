"""Core detection engine — renders SVGs in Chromium and measures text/rect overflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JS_DETECT = r"""
() => {
  const results = [];
  const svg = document.querySelector('svg');
  if (!svg) return { issues: [], viewBox: null };

  const vb = svg.viewBox.baseVal;
  const sr = svg.getBoundingClientRect();
  const svgW = vb.width || parseFloat(svg.getAttribute('width')) || svg.clientWidth;
  const svgH = vb.height || parseFloat(svg.getAttribute('height')) || svg.clientHeight;
  const scaleX = svgW / sr.width;
  const scaleY = svgH / sr.height;

  const allRects = [...svg.querySelectorAll('rect')];
  const boxes = [];

  for (let i = 0; i < allRects.length; i++) {
    const rect = allRects[i];
    const r = rect.getBoundingClientRect();
    if (r.width < 80 || r.height < 40) continue;
    boxes.push({
      el: rect,
      domIndex: i,
      px: { x: r.x, y: r.y, w: r.width, h: r.height },
      svg: {
        x: (r.x - sr.x) * scaleX,
        y: (r.y - sr.y) * scaleY,
        w: r.width * scaleX,
        h: r.height * scaleY
      },
      attrs: {
        x: rect.getAttribute('x') || '0',
        y: rect.getAttribute('y') || '0',
        width: rect.getAttribute('width') || '0',
        height: rect.getAttribute('height') || '0',
        fill: rect.getAttribute('fill') || '',
        class: rect.getAttribute('class') || ''
      }
    });
  }

  const pad = 3;

  for (const txt of svg.querySelectorAll('text')) {
    const bbox = txt.getBoundingClientRect();
    const cx = bbox.x + bbox.width / 2;
    const cy = bbox.y + bbox.height / 2;

    let parent = null;
    for (const box of boxes) {
      if (cx >= box.px.x && cx <= box.px.x + box.px.w &&
          cy >= box.px.y && cy <= box.px.y + box.px.h) {
        parent = box;
        break;
      }
    }

    if (!parent) {
      const edgePad = 4;
      const oL = bbox.x < sr.x + edgePad;
      const oR = bbox.x + bbox.width > sr.x + sr.width - edgePad;
      const oT = bbox.y < sr.y + edgePad;
      const oB = bbox.y + bbox.height > sr.y + sr.height - edgePad;
      if (oL || oR || oT || oB) {
        const dirs = [];
        if (oL) dirs.push('left');
        if (oR) dirs.push('right');
        if (oT) dirs.push('top');
        if (oB) dirs.push('bottom');
        results.push({
          type: 'text_viewbox',
          text: txt.textContent.trim().substring(0, 80),
          direction: dirs.join('+'),
          svg: _r({ x: (bbox.x - sr.x) * scaleX, y: (bbox.y - sr.y) * scaleY,
                    w: bbox.width * scaleX, h: bbox.height * scaleY }),
          parent: { viewBox: { w: Math.round(svgW), h: Math.round(svgH) } },
          fix: {
            // Right/bottom overflow can be fixed by enlarging the canvas.
            expand_viewbox_w: oR
              ? Math.round((bbox.x + bbox.width - sr.x) * scaleX - svgW + 4) : 0,
            expand_viewbox_h: oB
              ? Math.round((bbox.y + bbox.height - sr.y) * scaleY - svgH + 4) : 0
          }
        });
      }
      continue;
    }

    const oL = bbox.x < parent.px.x - pad;
    const oR = bbox.x + bbox.width > parent.px.x + parent.px.w + pad;
    const oT = bbox.y < parent.px.y - pad;
    const oB = bbox.y + bbox.height > parent.px.y + parent.px.h + pad;

    if (oL || oR || oT || oB) {
      const dirs = [];
      if (oL) dirs.push('left');
      if (oR) dirs.push('right');
      if (oT) dirs.push('top');
      if (oB) dirs.push('bottom');

      const extraW = Math.max(oL ? (parent.px.x - pad) - bbox.x : 0,
                               oR ? (bbox.x + bbox.width) - (parent.px.x + parent.px.w + pad) : 0);
      const extraH = Math.max(oT ? (parent.px.y - pad) - bbox.y : 0,
                               oB ? (bbox.y + bbox.height) - (parent.px.y + parent.px.h + pad) : 0);

      results.push({
        type: 'text_rect',
        text: txt.textContent.trim().substring(0, 80),
        direction: dirs.join('+'),
        svg: _r({ x: (bbox.x - sr.x) * scaleX, y: (bbox.y - sr.y) * scaleY,
                  w: bbox.width * scaleX, h: bbox.height * scaleY }),
        parent: {
          domIndex: parent.domIndex,
          svg: _r(parent.svg),
          attrs: parent.attrs
        },
        fix: {
          expand_w: Math.round(extraW * scaleX) + 4,
          expand_h: Math.round(extraH * scaleY) + 4
        }
      });
    }
  }

  const vpad = 2;
  for (const box of boxes) {
    const rx = box.svg.x;
    const ry = box.svg.y;
    const oL = rx < -vpad;
    const oR = rx + box.svg.w > svgW + vpad;
    const oT = ry < -vpad;
    const oB = ry + box.svg.h > svgH + vpad;

    if (oL || oR || oT || oB) {
      const dirs = [];
      if (oL) dirs.push('left');
      if (oR) dirs.push('right');
      if (oT) dirs.push('top');
      if (oB) dirs.push('bottom');

      results.push({
        type: 'rect_viewbox',
        text: 'rect[' + box.domIndex + '] fill="' + box.attrs.fill + '"',
        direction: 'viewbox+' + dirs.join('+'),
        svg: _r(box.svg),
        parent: { viewBox: svgW + 'x' + svgH },
        fix: {
          expand_viewbox_w: oR ? Math.round(rx + box.svg.w - svgW + 10) : 0,
          expand_viewbox_h: oB ? Math.round(ry + box.svg.h - svgH + 10) : 0
        }
      });
    }
  }

  function _r(o) {
    return { x: Math.round(o.x), y: Math.round(o.y),
             w: Math.round(o.w), h: Math.round(o.h) };
  }

  return { issues: results, viewBox: { w: Math.round(svgW), h: Math.round(svgH) } };
}
"""


@dataclass
class Issue:
    type: str  # 'text_rect' | 'rect_viewbox' | 'text_viewbox'
    text: str
    direction: str  # e.g. 'right+bottom' or 'viewbox+bottom'
    svg: dict  # {x, y, w, h} in SVG coordinate space
    parent: dict  # parent rect info or viewBox
    fix: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict) -> Issue:
        return cls(
            type=raw["type"],
            text=raw["text"],
            direction=raw["direction"],
            svg=raw["svg"],
            parent=raw.get("parent", {}),
            fix=raw.get("fix", {}),
        )


@dataclass
class CheckResult:
    path: Path
    issues: list[Issue]
    viewBox: dict | None

    @property
    def ok(self) -> bool:
        return len(self.issues) == 0


def check_svg(page, svg_path: Path) -> CheckResult:
    """Check a single SVG for text and rect overflow."""
    svg_path = Path(svg_path).resolve()
    file_url = svg_path.as_uri()
    page.goto(file_url, wait_until="load")
    page.wait_for_timeout(200)

    raw: dict[str, Any] = page.evaluate(JS_DETECT)
    issues = [Issue.from_raw(r) for r in raw.get("issues", [])]
    return CheckResult(path=svg_path, issues=issues, viewBox=raw.get("viewBox"))


def check_directory(
    svg_dir: Path | str,
    *,
    verbose: bool = False,
    json_out: Path | str | None = None,
) -> tuple[dict[str, CheckResult], int]:
    """Check all SVGs in a directory. Returns (results, total_issues)."""
    from playwright.sync_api import sync_playwright

    svg_dir = Path(svg_dir).resolve()
    if not svg_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {svg_dir}")

    svg_files = sorted(svg_dir.glob("*.svg"))
    if not svg_files:
        raise FileNotFoundError(f"No SVG files found in {svg_dir}")

    print(f"Checking {len(svg_files)} SVG files...")

    results: dict[str, CheckResult] = {}
    total_issues = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1200})

        for svg_path in svg_files:
            try:
                result = check_svg(page, svg_path)
            except Exception as e:
                print(f"  [ERR] {svg_path.name}: {e}")
                results[svg_path.name] = CheckResult(
                    path=svg_path, issues=[], viewBox=None
                )
                continue

            results[svg_path.name] = result
            if not result.ok:
                total_issues += len(result.issues)
                for issue in result.issues:
                    text_preview = issue.text[:40]
                    print(
                        f"  [!!] {svg_path.name}: "
                        f'"{text_preview}" overflows {issue.direction}'
                    )
                    if verbose and issue.type == "text_rect":
                        par = issue.parent
                        print(
                            f"       rect({par.get('attrs', {}).get('x', '?')},"
                            f"{par.get('attrs', {}).get('y', '?')} "
                            f"{par.get('attrs', {}).get('width', '?')}x"
                            f"{par.get('attrs', {}).get('height', '?')}) "
                            f"fix: expand +{issue.fix.get('expand_w', 0)}"
                            f"x+{issue.fix.get('expand_h', 0)}"
                        )
            elif verbose:
                print(f"  [OK] {svg_path.name}")

        browser.close()

    print(f"\n{'=' * 60}")
    print(
        f"Checked {len(svg_files)} files, found {total_issues} issues "
        f"in {sum(1 for r in results.values() if not r.ok)} files."
    )

    if json_out:
        out_path = Path(json_out)
        report = {}
        for name, result in results.items():
            if not result.ok:
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
        out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Report saved to {out_path}")

    return results, total_issues
