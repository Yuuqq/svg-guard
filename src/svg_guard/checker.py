"""Core detection engine — renders SVGs in Chromium and measures text/rect overflow."""

from __future__ import annotations

import fnmatch
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
  // A file the browser loaded as an XML document has an <svg> DOCUMENT
  // ELEMENT. If the XML failed to parse, Chromium swaps in an HTML error
  // page whose root is <html> and which contains a <parsererror> node — and
  // querySelector('svg') can still match a stray <svg> tag inside the error
  // text. So the only reliable "is this a real SVG" test is the document
  // element's tagName, not the presence of any <svg> descendant.
  const root = document.documentElement;
  if (!root || root.tagName.toLowerCase() !== 'svg'
      || document.querySelector('parsererror')) {
    return { issues: [], viewBox: null };
  }
  const svg = root;

  // Visible range is the viewBox rectangle, whose origin is often NOT 0
  // (Inkscape, cropped exports, "100 50 800 600"). Comparing against
  // [0, width] x [0, height] both misses content sitting outside a shifted
  // viewBox and invents overflow for content that is actually on-canvas.
  const vb = svg.viewBox.baseVal;
  const hasVb = !!(vb && vb.width > 0 && vb.height > 0);
  const vbX = hasVb ? vb.x : 0;
  const vbY = hasVb ? vb.y : 0;
  const svgW = hasVb
    ? vb.width
    : (parseFloat(svg.getAttribute('width')) || svg.clientWidth);
  const svgH = hasVb
    ? vb.height
    : (parseFloat(svg.getAttribute('height')) || svg.clientHeight);
  const vbR = vbX + svgW;
  const vbB = vbY + svgH;

  // Symbols, markers and <defs> are not drawn. Measuring them reports
  // overflow the user never sees (a long unused label, a hidden card).
  function skipEl(el) {
    if (el.closest('defs, symbol, marker, clipPath, mask, pattern')) return true;
    try {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return true;
    } catch (e) { /* detached — measure it anyway */ }
    return false;
  }

  // getCTM() maps into the nearest viewport, and a viewBox of
  // "min-x min-y width height" adds translate(-min-x, -min-y). Viewport
  // pixels therefore are not viewBox user units unless the origin is 0.
  // Put that origin (and any uniform viewBox scale) back.
  function absRootLen(name) {
    const raw = svg.getAttribute(name);
    if (!raw) return null;
    const m = String(raw).trim().match(/^([+-]?\d*\.?\d+)(px)?$/);
    return m ? parseFloat(m[1]) : null;
  }
  function rootUserFromViewport(px, py) {
    const viewportW = absRootLen('width') || svg.clientWidth || svgW;
    const viewportH = absRootLen('height') || svg.clientHeight || svgH;
    if (!(viewportW > 0) || !(viewportH > 0) || !(svgW > 0) || !(svgH > 0)) {
      return { x: px + vbX, y: py + vbY };
    }
    let scaleX = viewportW / svgW;
    let scaleY = viewportH / svgH;
    const par = (svg.getAttribute('preserveAspectRatio') || 'xMidYMid meet')
      .trim().split(/\s+/);
    const align = par[0] || 'xMidYMid';
    const mode = par[1] || 'meet';
    if (align !== 'none') {
      const s = mode === 'slice' ? Math.max(scaleX, scaleY) : Math.min(scaleX, scaleY);
      scaleX = s;
      scaleY = s;
    }
    const extraX = viewportW - svgW * scaleX;
    const extraY = viewportH - svgH * scaleY;
    function frac(tok) {
      if (tok === 'Min') return 0;
      if (tok === 'Max') return 1;
      return 0.5;
    }
    let dx = 0, dy = 0;
    if (align !== 'none' && align.length >= 8) {
      dx = extraX * frac(align.slice(1, 4));
      dy = extraY * frac(align.slice(5, 8));
    }
    return {
      x: (px - dx) / scaleX + vbX,
      y: (py - dy) / scaleY + vbY
    };
  }
  function localToUser(el, x, y) {
    const ctm = el.getCTM();
    if (!ctm) return { x: x, y: y };
    const px = ctm.a * x + ctm.c * y + ctm.e;
    const py = ctm.b * x + ctm.d * y + ctm.f;
    // Nested <svg> viewports stop getCTM before the root viewBox. Don't
    // apply the root origin there (nested_svg.svg only has to not crash).
    if (el.nearestViewportElement && el.nearestViewportElement !== svg) {
      return { x: px, y: py };
    }
    return rootUserFromViewport(px, py);
  }

  // getUserBox(el): axis-aligned box in the ROOT svg's user space (viewBox
  // coordinates), including transforms on the element and its ancestors.
  function getUserBox(el) {
    let bb;
    try {
      bb = el.getBBox();
    } catch (e) {
      return null;
    }
    const p1 = localToUser(el, bb.x, bb.y);
    const p2 = localToUser(el, bb.x + bb.width, bb.y);
    const p3 = localToUser(el, bb.x, bb.y + bb.height);
    const p4 = localToUser(el, bb.x + bb.width, bb.y + bb.height);
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
    if (skipEl(rect)) continue;
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
      },
      // A rect carrying a transform (or inside a transformed <g>) can't be
      // auto-fixed by editing its width/height attributes: those live in the
      // pre-transform local space, so adding N to width doesn't grow the
      // rendered (post-transform) box by N. The checker measures everything
      // in post-transform viewBox space, so the emitted expand deltas would
      // be applied to the wrong coordinate frame and the fix would silently
      // under- or over-shoot. Flag such rects so the fixer skips them with a
      // clear message instead of writing a wrong width.
      transformed: !!(rect.getAttribute('transform')
        || (rect.closest('[transform]') !== null))
    });
  }

  const pad = cfg.pad;

  function contains(box, x, y) {
    return x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h;
  }

  // Tightest rect that contains the anchor (the point the author placed),
  // then the bbox center. Document order used to win, so a full-canvas
  // background drawn first swallowed every label and card overflow was
  // never reported. A label wider than its card also has its bbox center
  // outside the card; the anchor is still inside, so we try that first.
  function pickParent(anchor, bbox) {
    function hitsAt(x, y) {
      const hits = [];
      for (const box of boxes) {
        if (contains(box.svg, x, y)) hits.push(box);
      }
      return hits;
    }
    let hits = anchor ? hitsAt(anchor.x, anchor.y) : [];
    if (!hits.length) {
      hits = hitsAt(bbox.x + bbox.w / 2, bbox.y + bbox.h / 2);
    }
    if (!hits.length) return null;
    hits.sort((a, b) => {
      const da = a.svg.w * a.svg.h - b.svg.w * b.svg.h;
      if (Math.abs(da) > 0.5) return da;
      return b.domIndex - a.domIndex;
    });
    return hits[0];
  }

  function getAnchor(el) {
    const rawX = el.getAttribute('x');
    const rawY = el.getAttribute('y');
    if (rawX == null || rawY == null) return null;
    const x = parseFloat(String(rawX).trim().split(/[\s,]+/)[0]);
    const y = parseFloat(String(rawY).trim().split(/[\s,]+/)[0]);
    if (!isFinite(x) || !isFinite(y)) return null;
    return localToUser(el, x, y);
  }

  function dirList(oL, oR, oT, oB) {
    const dirs = [];
    if (oL) dirs.push('left');
    if (oR) dirs.push('right');
    if (oT) dirs.push('top');
    if (oB) dirs.push('bottom');
    return dirs;
  }

  // extra: {foreign, clipped, clipDirection} for HTML inside <foreignObject>.
  // Plain <text> passes null and keeps the original text_rect / text_viewbox
  // payload (expand deltas, fixable, residual, transformed).
  function reportBox(text, bbox, parent, extra) {
    extra = extra || {};
    const foreign = extra.foreign || null;
    const clipped = !!extra.clipped;
    const clipDirection = extra.clipDirection || '';

    if (!parent) {
      // edgePad is a tolerance: spill by less than this and stay quiet.
      // (The old test used it as an inset, so labels sitting near the
      // canvas edge were flagged even though they were fully inside.)
      const edgePad = cfg.edgePad;
      const oL = bbox.x < vbX - edgePad;
      const oR = bbox.x + bbox.w > vbR + edgePad;
      const oT = bbox.y < vbY - edgePad;
      const oB = bbox.y + bbox.h > vbB + edgePad;
      if (!(oL || oR || oT || oB)) {
        if (!clipped) return;
        const grow = foreign && (foreign.expand_w > 0 || foreign.expand_h > 0);
        results.push({
          type: 'text_foreign',
          text: text,
          direction: clipDirection || 'right',
          svg: _r(bbox),
          parent: { viewBox: { w: Math.round(svgW), h: Math.round(svgH) } },
          fix: {
            expand_w: 0,
            expand_h: 0,
            fixable: !!grow,
            transformed: !!(foreign && foreign.transformed),
            foreign: foreign
          }
        });
        return;
      }
      const fix = {
        expand_viewbox_w: oR ? Math.round(bbox.x + bbox.w - vbR + cfg.fixPad) : 0,
        expand_viewbox_h: oB ? Math.round(bbox.y + bbox.h - vbB + cfg.fixPad) : 0
      };
      if (foreign) fix.foreign = foreign;
      results.push({
        type: 'text_viewbox',
        text: text,
        direction: dirList(oL, oR, oT, oB).join('+'),
        svg: _r(bbox),
        parent: { viewBox: { w: Math.round(svgW), h: Math.round(svgH) } },
        fix: fix
      });
      return;
    }

    const px = parent.svg;
    const oL = bbox.x < px.x - pad;
    const oR = bbox.x + bbox.w > px.x + px.w + pad;
    const oT = bbox.y < px.y - pad;
    const oB = bbox.y + bbox.h > px.y + px.h + pad;
    if (!(oL || oR || oT || oB || clipped)) return;

    const dirs = dirList(oL, oR, oT, oB);
    if (!dirs.length && clipDirection) {
      clipDirection.split('+').forEach((d) => { if (d) dirs.push(d); });
    }
    // Widening grows toward the bottom-right, so it covers right/bottom
    // overflow (including a label that sticks out of both sides). Pure
    // left/top stays unfixable. A transformed rect's width lives in
    // pre-transform space, so those deltas would land in the wrong frame.
    const expandW = oR
      ? Math.max(0, (bbox.x + bbox.w) - (px.x + px.w + pad)) + cfg.fixPad
      : 0;
    const expandH = oB
      ? Math.max(0, (bbox.y + bbox.h) - (px.y + px.h + pad)) + cfg.fixPad
      : 0;
    const parentTransformed = !!parent.transformed;
    const foTransformed = !!(foreign && foreign.transformed);
    const onlyClip = clipped && !(oL || oR || oT || oB);
    const rectFixable = (oR || oB) && !parentTransformed;
    const foFixable = !!(foreign && (foreign.expand_w > 0 || foreign.expand_h > 0));
    let fixable = onlyClip ? foFixable : rectFixable;
    if (clipped && foTransformed) fixable = false;
    const residual = [];
    if (oL) residual.push('left');
    if (oT) residual.push('top');
    const fix = {
      expand_w: rectFixable && !onlyClip ? Math.round(expandW) : 0,
      expand_h: rectFixable && !onlyClip ? Math.round(expandH) : 0,
      fixable: fixable,
      transformed: parentTransformed || foTransformed,
      residual: residual.join('+')
    };
    if (clipped && foTransformed) {
      fix.expand_w = 0;
      fix.expand_h = 0;
    }
    if (foreign) fix.foreign = foreign;
    results.push({
      type: onlyClip ? 'text_foreign' : 'text_rect',
      text: text,
      direction: dirs.join('+'),
      svg: _r(bbox),
      parent: {
        domIndex: parent.domIndex,
        svg: _r(parent.svg),
        attrs: parent.attrs
      },
      fix: fix
    });
  }

  // HTML clipped inside a <foreignObject>. Layout inside the FO is in CSS
  // pixels that match the FO's local user units (its width attribute is the
  // viewport width). scrollWidth - clientWidth is therefore already a user
  // unit delta. Scaling it by the attribute width makes a second pass grow
  // the box again instead of converging.
  function htmlOverflow(fo, foBox) {
    let left = 0, right = 0, top = 0, bottom = 0;
    let sample = '';
    const nodes = fo.querySelectorAll('*');
    for (let n = 0; n < nodes.length; n++) {
      const el = nodes[n];
      const sw = el.scrollWidth || 0;
      const sh = el.scrollHeight || 0;
      const cw = el.clientWidth || 0;
      const ch = el.clientHeight || 0;
      if (!sample && (el.textContent || '').trim()) sample = el.textContent;
      let style = null;
      if (cw > 0 && sw > cw + 1) {
        const extra = sw - cw;
        style = getComputedStyle(el);
        const align = ((style.textAlign || '') + ' ' + (style.justifyContent || '')).toLowerCase();
        if (align.indexOf('center') !== -1) {
          left = Math.max(left, extra / 2);
          right = Math.max(right, extra / 2);
        } else if (align.indexOf('right') !== -1 || align.indexOf('end') !== -1
                   || align.indexOf('flex-end') !== -1) {
          left = Math.max(left, extra);
        } else {
          right = Math.max(right, extra);
        }
      }
      if (ch > 0 && sh > ch + 1) {
        const extra = sh - ch;
        if (!style) style = getComputedStyle(el);
        const ai = (style.alignItems || '').toLowerCase();
        if (ai === 'center') {
          top = Math.max(top, extra / 2);
          bottom = Math.max(bottom, extra / 2);
        } else if (ai === 'flex-end' || ai === 'end') {
          top = Math.max(top, extra);
        } else {
          bottom = Math.max(bottom, extra);
        }
      }
    }
    return { left: left, right: right, top: top, bottom: bottom, text: sample };
  }

  function clipDirs(over) {
    const dirs = [];
    if (over.left > 0.5) dirs.push('left');
    if (over.right > 0.5) dirs.push('right');
    if (over.top > 0.5) dirs.push('top');
    if (over.bottom > 0.5) dirs.push('bottom');
    return dirs.join('+');
  }

  // Phase 1: text -> parent rect overflow.
  for (const txt of svg.querySelectorAll('text')) {
    if (skipEl(txt)) continue;
    const bbox = getUserBox(txt);
    if (!bbox) continue;
    reportBox(_clip(txt.textContent), bbox, pickParent(getAnchor(txt), bbox), null);
  }

  // Phase 1b: HTML labels inside <foreignObject>. The FO box itself often
  // sits inside the card while the text is clipped — getBBox cannot see that.
  const allFo = [...svg.querySelectorAll('foreignObject')];
  for (let fi = 0; fi < allFo.length; fi++) {
    const fo = allFo[fi];
    if (skipEl(fo)) continue;
    const foBox = getUserBox(fo);
    if (!foBox) continue;
    const over = htmlOverflow(fo, foBox);
    const clipped = over.left > 0.5 || over.right > 0.5
      || over.top > 0.5 || over.bottom > 0.5;
    const bbox = {
      x: foBox.x - over.left,
      y: foBox.y - over.top,
      w: foBox.w + over.left + over.right,
      h: foBox.h + over.top + over.bottom
    };
    const transformed = !!(fo.getAttribute('transform')
      || (fo.closest('[transform]') !== null));
    const foreign = {
      domIndex: fi,
      transformed: transformed,
      attrs: {
        x: fo.getAttribute('x') || '0',
        y: fo.getAttribute('y') || '0',
        width: fo.getAttribute('width') || '0',
        height: fo.getAttribute('height') || '0'
      },
      expand_w: (!transformed && over.right > 0.5)
        ? Math.round(over.right + cfg.fixPad) : 0,
      expand_h: (!transformed && over.bottom > 0.5)
        ? Math.round(over.bottom + cfg.fixPad) : 0
    };
    reportBox(
      _clip(over.text || fo.textContent),
      bbox,
      pickParent(getAnchor(fo), foBox),
      { foreign: foreign, clipped: clipped, clipDirection: clipDirs(over) }
    );
  }

  // Phase 2: rect -> viewBox overflow.
  const vpad = cfg.vpad;
  for (const box of boxes) {
    const rx = box.svg.x;
    const ry = box.svg.y;
    const oL = rx < vbX - vpad;
    const oR = rx + box.svg.w > vbR + vpad;
    const oT = ry < vbY - vpad;
    const oB = ry + box.svg.h > vbB + vpad;

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
          expand_viewbox_w: oR ? Math.round(rx + box.svg.w - vbR + cfg.vboxFixPad) : 0,
          expand_viewbox_h: oB ? Math.round(ry + box.svg.h - vbB + cfg.vboxFixPad) : 0
        }
      });
    }
  }

  // Phase 3: content coverage / misfit. A drawing that occupies only a tiny
  // off-center slice of the viewBox (e.g. content shrunk into the top-left
  // corner — a file that "opens mostly blank") slips through the two phases
  // above: the cards sit inside the viewBox and the text sits inside its card,
  // so neither overflow fires. We flag when the content is BOTH sparse AND
  // off-center — the AND avoids flagging a legitimately sparse but centered
  // layout (a label in the middle of a big canvas).
  //
  // Coverage = the SUM of each drawable's own bbox area ÷ viewBox area, NOT
  // the area of the union bounding box. The old union-bbox measure blew up on
  // any multi-element layout: two cards in opposite corners have a huge union
  // bbox (≈ the whole canvas) even though the actual ink covers a fraction of
  // it, so a genuinely spread-out diagram was never flagged, while a tight
  // cluster in one corner (the real "shrunk into a corner" case) measured the
  // same under both. Summing per-element area measures true ink coverage.
  var drawableSel = 'rect, text, path, circle, ellipse, line, polygon, '
                  + 'polyline, image, use';
  var drawable = svg.querySelectorAll(drawableSel);
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  var inkArea = 0;
  var found = false;
  for (var d = 0; d < drawable.length; d++) {
    if (skipEl(drawable[d])) continue;
    var db = getUserBox(drawable[d]);
    if (!db) continue;
    // Skip near-full-canvas rects: a background fill spanning the viewBox must
    // not mask genuinely tiny content behind it (it would force coverage ≈ 1).
    if (drawable[d].tagName.toLowerCase() === 'rect'
        && db.w >= svgW * cfg.bgRectRatio
        && db.h >= svgH * cfg.bgRectRatio) {
      continue;
    }
    found = true;
    inkArea += db.w * db.h;
    if (db.x < minX) minX = db.x;
    if (db.y < minY) minY = db.y;
    if (db.x + db.w > maxX) maxX = db.x + db.w;
    if (db.y + db.h > maxY) maxY = db.y + db.h;
  }

  if (found) {
    var cw = maxX - minX, ch = maxY - minY;
    var coverage = inkArea / (svgW * svgH);
    var cx = minX + cw / 2, cy = minY + ch / 2;
    // Normalized distance of the content centroid from the viewBox center: 0
    // means dead-center, ~1 means flush against an edge. The center is
    // (vbX + svgW/2, vbY + svgH/2), not (svgW/2, svgH/2).
    var offset = Math.max(
      Math.abs(cx - (vbX + svgW / 2)) / (svgW / 2),
      Math.abs(cy - (vbY + svgH / 2)) / (svgH / 2)
    );

    if (coverage < cfg.coverageThreshold
        && offset > cfg.centerOffsetThreshold) {
      // Crop coords: tighten the viewBox around the content, with a little pad
      // so the fixer doesn't shave off strokes at the very edge. Named cropPad
      // (not pad) to avoid clashing with the Phase 1 const of the same name.
      var cropPad = cfg.cropPad;
      var cx0 = Math.max(vbX, Math.round(minX - cropPad));
      var cy0 = Math.max(vbY, Math.round(minY - cropPad));
      var cw0 = Math.min(Math.round(vbR - cx0), Math.round(cw + 2 * cropPad));
      var ch0 = Math.min(Math.round(vbB - cy0), Math.round(ch + 2 * cropPad));
      results.push({
        type: 'content_misfit',
        text: 'content ' + Math.round(cw) + 'x' + Math.round(ch)
            + ' in ' + Math.round(svgW) + 'x' + Math.round(svgH) + ' viewBox',
        direction: 'viewbox+oversized',
        svg: _r({ x: minX, y: minY, w: cw, h: ch }),
        parent: { viewBox: svgW + 'x' + svgH },
        fix: {
          crop_x: cx0,
          crop_y: cy0,
          crop_w: cw0,
          crop_h: ch0,
          fixable: true
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

  return { issues: results, viewBox: {
    x: Math.round(vbX), y: Math.round(vbY),
    w: Math.round(svgW), h: Math.round(svgH)
  } };
}
"""


@dataclass
class Issue:
    type: str  # text_rect | rect_viewbox | text_viewbox | text_foreign | content_misfit
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
    # How far orphan text may spill past the viewBox edge before it counts.
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
    # Phase 3 (content misfit): flag when content occupies less than this
    # fraction of the viewBox area AND is off-center (see center_offset). The
    # AND avoids flagging legitimately sparse-but-centered layouts.
    coverage_threshold: float = 0.5
    center_offset_threshold: float = 0.5
    # A rect spanning at least this fraction of the viewBox on BOTH axes is
    # treated as a background fill and excluded from the content bbox (so a
    # full-canvas background doesn't mask genuinely tiny content).
    bg_rect_ratio: float = 0.9
    # Padding (user units) left around content when computing the crop viewBox
    # for a content_misfit fix, so strokes at the edge aren't shaved off.
    crop_pad: float = 8.0
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
            "coverageThreshold": self.coverage_threshold,
            "centerOffsetThreshold": self.center_offset_threshold,
            "bgRectRatio": self.bg_rect_ratio,
            "cropPad": self.crop_pad,
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

    # A file the browser can't parse as SVG (malformed XML, or not an SVG at
    # all) makes the probe return viewBox=null with zero issues — which the
    # old code returned as ok=True. That meant CI went green on corrupt SVGs.
    # Treat "no measurable SVG root" as a render error so the file is surfaced
    # (CLI exit 2, JSON error field, report banner) instead of passing silently.
    if raw.get("viewBox") is None:
        return CheckResult(
            path=svg_path,
            issues=[],
            viewBox=None,
            error="no renderable <svg> root (malformed XML or not an SVG file)",
        )

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


def _ignore_patterns(directory: Path) -> list[str]:
    """Glob patterns from ``directory/.svgguardignore`` (``#`` comments skipped)."""
    ignore = directory / ".svgguardignore"
    if not ignore.is_file():
        return []
    try:
        lines = ignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    patterns: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.replace("\\", "/"))
    return patterns


def _is_ignored(path: Path, root: Path, patterns: list[str]) -> bool:
    if not patterns:
        return False
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.name
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat):
            return True
        base = pat.rstrip("/")
        if rel == base or rel.startswith(base + "/"):
            return True
    return False


def collect_svgs(
    paths: list[Path | str],
    *,
    recursive: bool = False,
) -> list[tuple[Path, str]]:
    """Return ``(absolute path, report key)`` for every SVG under ``paths``.

    Each path is one ``.svg`` file or a directory. A directory contributes
    the ``.svg`` files directly inside it, or the whole tree when
    ``recursive`` is set. ``.svgguardignore`` in a scanned directory lists
    glob patterns and directory names to skip. The report key is the path
    relative to that directory, so two files that share a name stay distinct.

    Raises ``FileNotFoundError`` when a path is missing or nothing matches.
    """
    if not paths:
        raise FileNotFoundError("No SVG path given")

    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    used_keys: set[str] = set()

    def _add(path: Path, root: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        try:
            rel = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.name
        key = rel or path.name
        if key in used_keys:
            key = resolved.as_posix()
        used_keys.add(key)
        found.append((resolved, key))

    for raw in paths:
        target = Path(raw)
        if target.is_file():
            if target.suffix.lower() != ".svg":
                raise FileNotFoundError(f"Not an SVG file: {target}")
            root = target.parent
            if not _is_ignored(target, root, _ignore_patterns(root)):
                _add(target, root)
            continue
        if not target.is_dir():
            raise FileNotFoundError(f"Directory not found: {target}")
        patterns = _ignore_patterns(target)
        iterator = target.rglob("*") if recursive else target.iterdir()
        batch = [
            entry
            for entry in iterator
            if entry.is_file()
            and entry.suffix.lower() == ".svg"
            and not _is_ignored(entry, target, patterns)
        ]
        for entry in sorted(batch, key=lambda item: item.as_posix().lower()):
            _add(entry, target)

    if not found:
        where = ", ".join(str(p) for p in paths)
        raise FileNotFoundError(f"No SVG files found in {where}")
    return found


def check_paths(
    paths: list[Path | str],
    *,
    recursive: bool = False,
    verbose: bool = False,
    json_out: Path | str | None = None,
    config: DetectionConfig | None = None,
    runner: BrowserRunner | None = None,
) -> tuple[dict[str, CheckResult], int]:
    """Check every SVG under ``paths``. Returns ``(results, total_issues)``.

    ``results`` is keyed by the relative path ``collect_svgs`` chose.
    Pass ``runner=`` to reuse an already-launched browser.
    """
    cfg = config or (runner.config if runner else DetectionConfig())
    svg_files = collect_svgs(paths, recursive=recursive)

    logger.info("Checking %d SVG files...", len(svg_files))

    results: dict[str, CheckResult] = {}
    total_issues = 0
    total_errors = 0

    def _run(page) -> None:
        nonlocal total_issues, total_errors
        for svg_path, key in svg_files:
            try:
                result = check_svg(page, svg_path, config=cfg)
            except Exception as e:
                # Record the failure ON the result so callers (CLI, JSON report)
                # can tell a broken file from a clean one. ok becomes False.
                logger.warning("  [ERR] %s: %s", key, e)
                results[key] = CheckResult(
                    path=svg_path, issues=[], viewBox=None, error=str(e)
                )
                total_errors += 1
                continue

            results[key] = result
            if result.error is not None:
                # check_svg returned a render error (e.g. no parseable <svg>
                # root) rather than raising. Surface it exactly like the
                # exception path above so the CLI/CI log shows [ERR] and the
                # file is never mistaken for a clean pass.
                logger.warning("  [ERR] %s: %s", key, result.error)
                total_errors += 1
                continue
            if not result.ok:
                total_issues += len(result.issues)
                for issue in result.issues:
                    text_preview = issue.text[:40]
                    logger.info(
                        '  [!!] %s: "%s" overflows %s',
                        key,
                        text_preview,
                        issue.direction,
                    )
                    if verbose and issue.type in ("text_rect", "text_foreign"):
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
                logger.info("  [OK] %s", key)

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


def check_directory(
    svg_dir: Path | str,
    *,
    verbose: bool = False,
    json_out: Path | str | None = None,
    config: DetectionConfig | None = None,
    runner: BrowserRunner | None = None,
    recursive: bool = False,
) -> tuple[dict[str, CheckResult], int]:
    """Check a directory of SVGs, or one SVG file. Returns (results, total_issues).

    Pass ``runner=`` to reuse an already-launched browser across calls
    (avoids Chromium's ~1s startup per invocation). When omitted, a
    short-lived runner is created for this call only. ``recursive`` walks
    subdirectories. See ``collect_svgs`` for ``.svgguardignore``.
    """
    return check_paths(
        [svg_dir],
        recursive=recursive,
        verbose=verbose,
        json_out=json_out,
        config=config,
        runner=runner,
    )
