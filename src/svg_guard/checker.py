"""Core detection engine — renders SVGs in Chromium and measures text/rect overflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Module logger. The CLI attaches a handler so users see the same progress
# output as before; library callers get no handler by default and the module
# is silent (call check_directory without dragging print side-effects along).
logger = logging.getLogger("svg_guard")

# Detection thresholds. Exposed via DetectionConfig (Python) and injected into
# the JS probe as a JSON object so they are no longer hard-coded magic numbers
# inside the JS string. All values are in SVG USER UNITS (viewBox space), not
# CSS pixels — measuring in user units is what makes the results independent of
# the browser viewport size (the old code scaled by viewport and was wrong
# whenever the viewBox aspect ratio differed from the viewport's).
JS_DETECT = r"""
(cfg) => {
  const results = [];
  const svg = document.querySelector('svg');
  if (!svg) return { issues: [], viewBox: null };

  const vb = svg.viewBox.baseVal;
  const svgW = vb.width || parseFloat(svg.getAttribute('width')) || svg.clientWidth;
  const svgH = vb.height || parseFloat(svg.getAttribute('height')) || svg.clientHeight;

  // getUserBox(el): return the element's axis-aligned bounding box in the
  // ROOT svg's user coordinate system (viewBox space), INCLUDING any
  // transforms applied to the element or its ancestors.
  //
  // getBoundingClientRect() (the old approach) returns CSS pixels and is
  // distorted by viewport scaling — when the viewBox aspect ratio differs
  // from the viewport's, scaleX != scaleY and every downstream comparison is
  // skewed. getBBox() returns user units in the element's LOCAL coordinate
  // system (no transforms); we then fold in getCTM() (local -> root svg user
  // units, accumulating all transforms incl. nested <svg> and rotate) to land
  // in a single consistent space. No viewport scaling is involved at all.
  function getUserBox(el) {
    let bb;
    try {
      bb = el.getBBox();  // local user units, no transform
    } catch (e) {
      return null;  // not rendered / detached — skip this element
    }
    const ctm = el.getCTM();  // null when el IS the viewport (root svg)
    if (!ctm) return { x: bb.x, y: bb.y, w: bb.width, h: bb.height };
    // getCTM() returns an SVGMatrix (a,b,c,d,e,f) with NO transformPoint
    // method (that's on DOMMatrix). Apply it manually to each corner:
    //   x' = a*x + c*y + e ,  y' = b*x + d*y + f
    const tx = (x, y) => ({ x: ctm.a * x + ctm.c * y + ctm.e,
                            y: ctm.b * x + ctm.d * y + ctm.f });
    const p1 = tx(bb.x, bb.y);
    const p2 = tx(bb.x + bb.width, bb.y);
    const p3 = tx(bb.x, bb.y + bb.height);
    const p4 = tx(bb.x + bb.width, bb.y + bb.height);
    const xs = [p1.x, p2.x, p3.x, p4.x];
    const ys = [p1.y, p2.y, p3.y, p4.y];
    const x = Math.min(...xs), y = Math.min(...ys);
    return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
  }

  // Collect candidate container rects (in viewBox user units).
  const allRects = [...svg.querySelectorAll('rect')];
  const boxes = [];
  for (let i = 0; i < allRects.length; i++) {
    const rect = allRects[i];
    const box = getUserBox(rect);
    if (!box) continue;
    // Skip tiny rects (decorative dots, dividers) — threshold is in user units
    // so it doesn't shift with viewport zoom like the old CSS-pixel filter.
    if (box.w < cfg.minRectW || box.h < cfg.minRectH) continue;
    boxes.push({
      domIndex: i,
      svg: box,
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

  const pad = cfg.pad;

  // Phase 1: text -> parent rect overflow.
  for (const txt of svg.querySelectorAll('text')) {
    const bbox = getUserBox(txt);
    if (!bbox) continue;
    const cx = bbox.x + bbox.w / 2;
    const cy = bbox.y + bbox.h / 2;

    // Match text to its containing rect by center-point containment.
    // All comparisons are now in viewBox user units (no viewport scaling).
    let parent = null;
    for (const box of boxes) {
      if (cx >= box.svg.x && cx <= box.svg.x + box.svg.w &&
          cy >= box.svg.y && cy <= box.svg.y + box.svg.h) {
        parent = box;
        break;
      }
    }

    if (!parent) {
      // Orphan text: no containing rect. Only flag if it spills the viewBox.
      const edgePad = cfg.edgePad;
      const oL = bbox.x < 0 + edgePad;
      const oR = bbox.x + bbox.w > svgW - edgePad;
      const oT = bbox.y < 0 + edgePad;
      const oB = bbox.y + bbox.h > svgH - edgePad;
      if (oL || oR || oT || oB) {
        const dirs = [];
        if (oL) dirs.push('left');
        if (oR) dirs.push('right');
        if (oT) dirs.push('top');
        if (oB) dirs.push('bottom');
        results.push({
          type: 'text_viewbox',
          text: _clip(txt.textContent),
          direction: dirs.join('+'),
          svg: _r(bbox),
          parent: { viewBox: { w: Math.round(svgW), h: Math.round(svgH) } },
          fix: {
            // Only right/bottom overflow can be resolved by enlarging the
            // canvas; left/top needs the text moved, which we can't do safely.
            expand_viewbox_w: oR ? Math.round(bbox.x + bbox.w - svgW + cfg.fixPad) : 0,
            expand_viewbox_h: oB ? Math.round(bbox.y + bbox.h - svgH + cfg.fixPad) : 0
          }
        });
      }
      continue;
    }

    const px = parent.svg;
    const oL = bbox.x < px.x - pad;
    const oR = bbox.x + bbox.w > px.x + px.w + pad;
    const oT = bbox.y < px.y - pad;
    const oB = bbox.y + bbox.h > px.y + px.h + pad;

    if (oL || oR || oT || oB) {
      const dirs = [];
      if (oL) dirs.push('left');
      if (oR) dirs.push('right');
      if (oT) dirs.push('top');
      if (oB) dirs.push('bottom');

      // Expanding a rect's width/height grows it toward the bottom-right.
      // That can only COVER right/bottom overflow; left/top overflow (text
      // starts before the rect's left/top edge) would remain no matter how
      // wide/tall the rect gets, so re-checking after a fix would re-report
      // the same left/top issue forever (a fix loop). Mark such issues as
      // not auto-fixable so the fixer skips them with a clear message
      // instead of churning the file.
      const fixable = !oL && !oT;
      const extraW = fixable
        ? Math.max(0, (bbox.x + bbox.w) - (px.x + px.w + pad))
        : 0;
      const extraH = fixable
        ? Math.max(0, (bbox.y + bbox.h) - (px.y + px.h + pad))
        : 0;

      results.push({
        type: 'text_rect',
        text: _clip(txt.textContent),
        direction: dirs.join('+'),
        svg: _r(bbox),
        parent: {
          domIndex: parent.domIndex,
          svg: _r(parent.svg),
          attrs: parent.attrs
        },
        fix: {
          expand_w: Math.round(extraW) + (fixable ? cfg.fixPad : 0),
          expand_h: Math.round(extraH) + (fixable ? cfg.fixPad : 0),
          fixable: fixable
        }
      });
    }
  }

  // Phase 2: rect -> viewBox overflow.
  const vpad = cfg.vpad;
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
          // Only right/bottom overflow is canvas-fixable.
          expand_viewbox_w: oR ? Math.round(rx + box.svg.w - svgW + cfg.vboxFixPad) : 0,
          expand_viewbox_h: oB ? Math.round(ry + box.svg.h - svgH + cfg.vboxFixPad) : 0
        }
      });
    }
  }

  // Clip text to a safe number of code units, splitting on surrogate pairs so
  // we never emit a lone half of an emoji into the JSON report.
  function _clip(s) {
    const t = (s || '').trim();
    return Array.from(t).slice(0, 80).join('');
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
    # When rendering this file failed, ``error`` holds the message and
    # ``issues`` is empty. ``ok`` is False in that case so a broken file is
    # never mistaken for a clean one. (Previously a render failure was stored
    # as an empty-issues CheckResult with ok=True, making CI silently green on
    # corrupt SVGs — see audit finding A2.)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.issues) == 0


@dataclass
class DetectionConfig:
    """Detection thresholds, all in SVG USER UNITS (viewBox space).

    Centralising these (instead of hard-coding them inside the JS probe)
    makes the detector tunable for tight icon layouts, poster-size diagrams,
    etc. Defaults match the behaviour before this config existed.
    """

    # Tolerance when deciding whether text overflows its parent rect.
    pad: float = 3.0
    # Tolerance for orphan text spilling the viewBox edge.
    edge_pad: float = 4.0
    # Tolerance for a rect itself overflowing the viewBox.
    vpad: float = 2.0
    # Extra px added to a card-expand fix so the text clears the edge.
    fix_pad: float = 4.0
    # Extra px added to a viewBox-expand fix (rect_viewbox).
    vbox_fix_pad: float = 10.0
    # Rects smaller than this (in user units) are treated as decorative and
    # ignored as candidate parent containers.
    min_rect_w: float = 80.0
    min_rect_h: float = 40.0
    # Browser viewport for rendering. Large enough that typical viewBoxes are
    # rendered near 1:1, minimising subpixel rounding.
    viewport_w: int = 1600
    viewport_h: int = 1200

    def as_js(self) -> dict:
        """Serialize to the plain object the JS probe expects."""
        return {
            "pad": self.pad,
            "edgePad": self.edge_pad,
            "vpad": self.vpad,
            "fixPad": self.fix_pad,
            "vboxFixPad": self.vbox_fix_pad,
            "minRectW": self.min_rect_w,
            "minRectH": self.min_rect_h,
        }


def check_svg(
    page, svg_path: Path, *, config: DetectionConfig | None = None
) -> CheckResult:
    """Check a single SVG for text and rect overflow."""
    cfg = config or DetectionConfig()
    svg_path = Path(svg_path).resolve()
    file_url = svg_path.as_uri()
    page.goto(file_url, wait_until="load")
    # Wait for fonts to settle instead of a fixed 200ms sleep. SVGs may pull
    # webfonts via @font-face; measuring before they load gives wrong widths
    # (especially CJK). document.fonts.ready is a Promise that resolves once
    # all fonts in use have loaded (or failed); page.evaluate awaits it.
    # Wrapped so a missing API (very old Chromium) doesn't break the check.
    try:
        page.evaluate(
            "() => (document.fonts && document.fonts.ready) || Promise.resolve()"
        )
    except Exception:
        pass  # non-fatal: measure with whatever fonts are available

    raw: dict[str, Any] = page.evaluate(JS_DETECT, cfg.as_js())
    issues = [Issue.from_raw(r) for r in raw.get("issues", [])]
    return CheckResult(path=svg_path, issues=issues, viewBox=raw.get("viewBox"))


class BrowserRunner:
    """Owns a Playwright browser/page lifecycle, reusable across calls.

    Using this as a context manager (``with BrowserRunner(config) as r:``)
    lets callers check many directories/files without relaunching Chromium
    for each one. ``check_directory`` accepts a runner via the ``runner``
    kwarg; when omitted it creates one internally (backwards compatible).

    Kept as a thin sync wrapper on purpose: a full async rewrite for
    parallel rendering is a larger, riskier change tracked separately.
    """

    def __init__(self, config: DetectionConfig | None = None) -> None:
        self.config = config or DetectionConfig()
        self._pw = None
        self._browser = None
        self.page = None

    def __enter__(self) -> "BrowserRunner":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self.page = self._browser.new_page(
            viewport={
                "width": self.config.viewport_w,
                "height": self.config.viewport_h,
            }
        )
        return self

    def __exit__(self, *exc) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self.page = None
        self._browser = None
        self._pw = None


def check_directory(
    svg_dir: Path | str,
    *,
    verbose: bool = False,
    json_out: Path | str | None = None,
    config: DetectionConfig | None = None,
    runner: BrowserRunner | None = None,
) -> tuple[dict[str, CheckResult], int]:
    """Check all SVGs in a directory. Returns (results, total_issues).

    Pass ``runner=`` to reuse an already-launched browser across calls
    (avoids Chromium's ~1s startup per invocation). When omitted, a
    short-lived runner is created for this call only.
    """
    cfg = config or (runner.config if runner else DetectionConfig())
    svg_dir = Path(svg_dir).resolve()
    if not svg_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {svg_dir}")

    svg_files = sorted(svg_dir.glob("*.svg"))
    if not svg_files:
        raise FileNotFoundError(f"No SVG files found in {svg_dir}")

    logger.info("Checking %d SVG files...", len(svg_files))

    results: dict[str, CheckResult] = {}
    total_issues = 0
    total_errors = 0

    def _run(page) -> None:
        nonlocal total_issues, total_errors
        for svg_path in svg_files:
            try:
                result = check_svg(page, svg_path, config=cfg)
            except Exception as e:
                # Record the failure ON the result so callers (CLI, JSON report)
                # can tell a broken file from a clean one. ok becomes False.
                logger.warning("  [ERR] %s: %s", svg_path.name, e)
                results[svg_path.name] = CheckResult(
                    path=svg_path, issues=[], viewBox=None, error=str(e)
                )
                total_errors += 1
                continue

            results[svg_path.name] = result
            if not result.ok:
                total_issues += len(result.issues)
                for issue in result.issues:
                    text_preview = issue.text[:40]
                    logger.info(
                        '  [!!] %s: "%s" overflows %s',
                        svg_path.name,
                        text_preview,
                        issue.direction,
                    )
                    if verbose and issue.type == "text_rect":
                        par = issue.parent
                        logger.info(
                            "       rect(%s,%s %sx%s) fix: expand +%sx+%s",
                            par.get("attrs", {}).get("x", "?"),
                            par.get("attrs", {}).get("y", "?"),
                            par.get("attrs", {}).get("width", "?"),
                            par.get("attrs", {}).get("height", "?"),
                            issue.fix.get("expand_w", 0),
                            issue.fix.get("expand_h", 0),
                        )
            elif verbose:
                logger.info("  [OK] %s", svg_path.name)

    if runner is not None:
        _run(runner.page)
    else:
        with BrowserRunner(cfg) as r:
            _run(r.page)

    files_with_problems = sum(1 for r in results.values() if not r.ok)
    logger.info("%s", "=" * 60)
    logger.info(
        "Checked %d files, found %d issues in %d files%s.",
        len(svg_files),
        total_issues,
        files_with_problems,
        f" ({total_errors} failed to render)" if total_errors else "",
    )

    if json_out:
        # Delegate to report.py so the JSON schema lives in one place.
        from .report import write_json_report

        write_json_report(results, json_out)
        logger.info("Report saved to %s", Path(json_out))

    return results, total_issues
