"""Auto-fix engine — repairs overflow issues using regex-based SVG patching."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ._io import read_svg, write_text_atomic
from .checker import Issue

# Matches a numeric length with an optional unit. Bare numbers, px, pt, em and %
# are recognized; everything else (auto, none, calc(...), ...) is rejected.
_LEN_RE = re.compile(r"^\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*(px|pt|em|%)?\s*$")

# Conversion of physical units to CSS pixels (96 DPI reference).
_PT_TO_PX = 96.0 / 72.0

# Default font size in px, used to resolve `em` when no real font-size is known.
_DEFAULT_FONT_PX = 16.0


def fix_svg(
    svg_path: Path | str,
    issues: list[Issue],
    *,
    backup: bool = True,
    dry_run: bool = False,
) -> list[str]:
    """Auto-fix overflow issues in an SVG file.

    Returns a list of human-readable change descriptions. Creates a ``.bak``
    backup by default; if a backup already exists a numeric suffix (``.bak.1``,
    ``.bak.2`` …) is used so the original is never clobbered.
    """
    svg_path = Path(svg_path)
    content = read_svg(svg_path)
    changes: list[str] = []

    if backup and not dry_run:
        _safe_backup(svg_path)

    # Fix viewBox overflow first (changes the canvas, not individual elements).
    # Both rect_viewbox and text_viewbox (orphan text) widen the canvas.
    # Deltas on the same edge collapse to the larger one.

    # content_misfit (Phase 3) crops the viewBox onto the content. Expand
    # deltas were measured against the pre-crop edge, so remember that edge
    # and, after a successful crop, push the new right/bottom out to
    # old_edge + delta instead of adding the delta to the cropped size.
    old_vb = _read_viewbox(content)
    cropped = False
    for issue in issues:
        if issue.type != "content_misfit":
            continue
        crop_x = issue.fix.get("crop_x")
        crop_y = issue.fix.get("crop_y")
        crop_w = issue.fix.get("crop_w")
        crop_h = issue.fix.get("crop_h")
        if None in (crop_x, crop_y, crop_w, crop_h):
            changes.append("content_misfit skipped: missing crop coords in issue")
            continue
        content, change = _crop_viewbox(
            content, float(crop_x), float(crop_y), float(crop_w), float(crop_h)
        )
        if change:
            changes.append(change)
            cropped = change.startswith("viewBox cropped to")
        break  # first content_misfit establishes the cropped baseline

    vb_dw, vb_dh = 0.0, 0.0
    for issue in issues:
        if issue.type not in ("rect_viewbox", "text_viewbox"):
            continue
        dw = issue.fix.get("expand_viewbox_w", 0) or 0
        dh = issue.fix.get("expand_viewbox_h", 0) or 0
        if dw == 0 and dh == 0 and issue.type == "text_viewbox":
            # Pure left/top overflow can't be fixed by enlarging the canvas.
            changes.append(
                f'orphan text "{(issue.text or "").strip()[:40]}" skipped: '
                f"left/top overflow needs manual repositioning"
            )
            continue
        # Two cards past the same edge need the larger delta, not the sum.
        vb_dw = max(vb_dw, float(dw))
        vb_dh = max(vb_dh, float(dh))

    if vb_dw > 0 or vb_dh > 0:
        if cropped and old_vb is not None:
            content, change = _extend_cropped_viewbox(content, old_vb, vb_dw, vb_dh)
        else:
            content, change = _expand_viewbox(content, vb_dw, vb_dh)
        if change:
            changes.append(change)

    # Fix card text overflow (widens/expands rects). Multiple issues can hit
    # the same rect; collapse them by parent identity, taking the max delta in
    # each direction so nothing is silently under-fixed (and so the second
    # issue isn't left looking for an already-rewritten width="...").
    #
    # A text_rect issue is fixable whenever the checker emitted a non-zero
    # expand_w/expand_h (right/bottom component). A text wider than its rect
    # overflows BOTH left and right; widening the rect covers the right side,
    # which resolves the width problem — so we no longer skip those wholesale.
    # Only a PURE left/top overflow (no right/bottom component) is skipped,
    # since widening can never reach text that starts before the rect's
    # left/top edge. Residual left/top components are surfaced in the message.
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for issue in issues:
        if issue.type != "text_rect":
            continue
        ew = issue.fix.get("expand_w", 0) or 0
        eh = issue.fix.get("expand_h", 0) or 0
        if ew <= 0 and eh <= 0:
            # Distinguish the two unfixable causes so the user knows why.
            if issue.fix.get("transformed"):
                # Rect is transformed (or inside a transformed group): editing
                # width/height in pre-transform local space wouldn't grow the
                # rendered box by the measured amount. Skip rather than write
                # a wrong width.
                changes.append(
                    f'text "{(issue.text or "").strip()[:40]}" skipped: '
                    f"parent rect is transformed; edit width/height manually"
                )
            else:
                # Pure left/top overflow: nothing the rect can grow into.
                changes.append(
                    f'text "{(issue.text or "").strip()[:40]}" skipped: '
                    f"left/top overflow needs manual repositioning"
                )
            continue
        attrs = issue.parent.get("attrs", {})
        dom_index = issue.parent.get("domIndex")
        # Group by domIndex when available (uniquely identifies the rect even
        # if several share identical x/y); fall back to (x, y) otherwise.
        key = (
            dom_index
            if dom_index is not None
            else (attrs.get("x", ""), attrs.get("y", ""))
        )
        slot = grouped.setdefault(
            key,
            {
                "expand_w": 0.0,
                "expand_h": 0.0,
                "attrs": attrs,
                "dom_index": dom_index,
            },
        )
        if key not in order:
            order.append(key)
        slot["expand_w"] = max(slot["expand_w"], issue.fix.get("expand_w", 0) or 0)
        slot["expand_h"] = max(slot["expand_h"], issue.fix.get("expand_h", 0) or 0)

    for key in order:
        slot = grouped[key]
        content, change = _fix_card(
            content,
            slot["attrs"],
            slot["expand_w"],
            slot["expand_h"],
            dom_index=slot["dom_index"],
        )
        if change:
            changes.append(change)

    # HTML labels clipped by a <foreignObject> (draw.io and similar). Growing
    # the parent card does not unclip the HTML; the foreignObject's own
    # width/height has to grow too. Group by element so two labels in one
    # box take the larger delta.
    fo_grouped: dict[tuple, dict] = {}
    fo_order: list[tuple] = []
    for issue in issues:
        foreign = issue.fix.get("foreign") if isinstance(issue.fix, dict) else None
        if not isinstance(foreign, dict):
            if issue.type == "text_foreign":
                changes.append(
                    f'text "{(issue.text or "").strip()[:40]}" skipped: '
                    f"foreignObject overflow needs a manual edit"
                )
            continue
        ew = foreign.get("expand_w", 0) or 0
        eh = foreign.get("expand_h", 0) or 0
        if ew <= 0 and eh <= 0:
            # A text_rect that also mentions the foreignObject already logged
            # its own skip. Only text_foreign has nothing else to say.
            if issue.type != "text_foreign":
                continue
            if foreign.get("transformed") or issue.fix.get("transformed"):
                changes.append(
                    f'text "{(issue.text or "").strip()[:40]}" skipped: '
                    f"foreignObject is transformed; edit width/height manually"
                )
            else:
                changes.append(
                    f'text "{(issue.text or "").strip()[:40]}" skipped: '
                    f"left/top overflow needs manual repositioning"
                )
            continue
        attrs = foreign.get("attrs") or {}
        dom_index = foreign.get("domIndex")
        key = (
            dom_index
            if dom_index is not None
            else (attrs.get("x", ""), attrs.get("y", ""))
        )
        slot = fo_grouped.setdefault(
            key,
            {
                "expand_w": 0.0,
                "expand_h": 0.0,
                "attrs": attrs,
                "dom_index": dom_index,
            },
        )
        if key not in fo_order:
            fo_order.append(key)
        slot["expand_w"] = max(slot["expand_w"], ew)
        slot["expand_h"] = max(slot["expand_h"], eh)

    for key in fo_order:
        slot = fo_grouped[key]
        content, change = _fix_card(
            content,
            slot["attrs"],
            slot["expand_w"],
            slot["expand_h"],
            dom_index=slot["dom_index"],
            tag="foreignObject",
        )
        if change and "skipped" not in change:
            content = _sync_inner_label_px(
                content,
                slot["dom_index"],
                slot["attrs"],
                slot["expand_w"],
                slot["expand_h"],
            )
        if change:
            changes.append(change)

    if changes and not dry_run:
        write_text_atomic(svg_path, content)

    return changes


def _safe_backup(svg_path: Path) -> None:
    """Copy ``svg_path`` to a backup, numbering past backups if one exists."""
    base = svg_path.with_suffix(".svg.bak")
    target = base
    n = 1
    while target.exists():
        target = svg_path.with_suffix(f".svg.bak.{n}")
        n += 1
    shutil.copy2(svg_path, target)


def _parse_len(value: str, ref_px: float = _DEFAULT_FONT_PX) -> float | None:
    """Parse an SVG/CSS length into absolute pixels.

    Recognizes bare numbers, ``px``, ``pt`` and ``em``. Returns ``None`` for
    percentages, ``auto``, ``none`` and anything else that can't be converted
    to an absolute length.
    """
    if value is None:
        return None
    m = _LEN_RE.match(str(value))
    if not m:
        return None
    if m.group(2) == "%":
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if unit == "pt":
        return num * _PT_TO_PX
    if unit == "em":
        return num * ref_px
    return num  # bare number or px


def _read_viewbox(content: str) -> tuple[float, float, float, float] | None:
    """Return the first viewBox as (x, y, w, h), or None if it can't be parsed."""
    match = re.search(
        r'viewBox\s*=\s*"([^"]*)"|viewBox\s*=\s*\'([^\']*)\'',
        content,
    )
    if not match:
        return None
    inner = match.group(1) if match.group(1) is not None else match.group(2)
    parts = inner.split()
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (float(part) for part in parts)
    except ValueError:
        return None
    return x, y, w, h


def _extend_cropped_viewbox(
    content: str,
    old: tuple[float, float, float, float],
    delta_w: float,
    delta_h: float,
) -> tuple[str, str | None]:
    """Keep overflow that a crop clamped away.

    ``delta_*`` is how far geometry passed the pre-crop right/bottom edge.
    The cropped viewBox keeps its new origin. Its right/bottom moves out to
    at least ``old_edge + delta``, which is where that geometry ends.
    """
    current = _read_viewbox(content)
    if current is None:
        return _expand_viewbox(content, delta_w, delta_h)
    ox, oy, ow, oh = old
    cx, cy, cw, ch = current
    right = cx + cw
    bottom = cy + ch
    if delta_w > 0:
        right = max(right, ox + ow + delta_w)
    if delta_h > 0:
        bottom = max(bottom, oy + oh + delta_h)
    new_w = right - cx
    new_h = bottom - cy
    if new_w <= cw + 0.5 and new_h <= ch + 0.5:
        return content, None
    updated, change = _crop_viewbox(content, cx, cy, new_w, new_h)
    if not change or not change.startswith("viewBox cropped to"):
        return updated, change
    return updated, f"viewBox extended after crop to {new_w:.0f}x{new_h:.0f}"


def _expand_viewbox(
    content: str, delta_w: float, delta_h: float
) -> tuple[str, str | None]:
    """Widen the viewBox and the root <svg> width/height by the given deltas.

    Handles both double- and single-quoted ``viewBox`` attributes. When the
    root ``<svg>`` has no ``width``/``height`` attribute, a matching pair is
    synthesised from the new viewBox so the rendered canvas grows with it
    (otherwise the larger viewBox would just be scaled into the old box and
    the whole diagram would shrink — a silent visual regression).
    """
    if delta_w <= 0 and delta_h <= 0:
        return content, None

    new_content = content
    skipped: list[str] = []
    # Parsed new viewBox dims, populated by replace_vb so the width/height
    # injection below can reuse them without re-parsing.
    new_vb_w: float | None = None
    new_vb_h: float | None = None

    def replace_vb(quote: str, inner: str) -> str:
        nonlocal new_vb_w, new_vb_h
        parts = inner.split()
        if len(parts) != 4:
            skipped.append("viewBox (unrecognized format)")
            return f"viewBox={quote}{inner}{quote}"
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            skipped.append(f"viewBox={quote}{inner}{quote}")
            return f"viewBox={quote}{inner}{quote}"
        w = nums[2] + delta_w
        h = nums[3] + delta_h
        new_vb_w, new_vb_h = w, h
        return f"viewBox={quote}{nums[0]:.0f} {nums[1]:.0f} {w:.0f} {h:.0f}{quote}"

    def vb_callback(m: "re.Match[str]") -> str:
        if m.group(1) is not None:
            return replace_vb('"', m.group(1))
        return replace_vb("'", m.group(2))

    # Match viewBox with either double- or single-quoted value (XML allows
    # both) and preserve the original quote style on rewrite.
    new_content = re.sub(
        r'viewBox\s*=\s*"([^"]*)"|viewBox\s*=\s*\'([^\']*)\'',
        vb_callback,
        new_content,
        count=1,
    )

    # Sync the root <svg> width/height to the new viewBox:
    #  - if the attr exists, add the same delta (canvas grows with viewBox);
    #  - if it is missing, inject it set to the new viewBox dim (otherwise the
    #    bigger viewBox would be squashed into the default 300x150 box).
    parts_msg: list[str] = []
    if delta_w > 0:
        new_content, mode = _sync_root_svg_dim(new_content, "width", delta_w, new_vb_w)
        parts_msg.append(_dim_msg("w", delta_w, mode))
    if delta_h > 0:
        new_content, mode = _sync_root_svg_dim(new_content, "height", delta_h, new_vb_h)
        parts_msg.append(_dim_msg("h", delta_h, mode))

    msg = f"viewBox expanded ({', '.join(parts_msg)})"
    if skipped:
        msg += f"; skipped {', '.join(skipped)}"
    return new_content, msg


def _sync_root_svg_dim(
    content: str, attr: str, delta: float, fallback_value: float | None
) -> tuple[str, str]:
    """Grow or inject ``attr`` on the root <svg> to match the viewBox growth.

    Returns (new_content, mode) where mode is one of:
      ``"add"``     — existing numeric attr incremented by delta
      ``"inject"``  — attr was missing, set to fallback_value (the new viewBox dim)
      ``"skip"``    — attr present but non-numeric (e.g. "100%"), left untouched
    """
    svg_match = re.search(r"<svg\b[^>]*>", content)
    if not svg_match:
        return content, "skip"
    svg_tag = svg_match.group(0)

    m = re.search(rf'\b{attr}\s*=\s*"([^"]*)"', svg_tag)
    if not m:
        # Missing — inject from the new viewBox dim if we know it.
        if fallback_value is None:
            return content, "skip"
        insert_at = svg_match.end()
        injected = f' {attr}="{fallback_value:.0f}"'
        new_content = content[:insert_at] + injected + content[insert_at:]
        return new_content, "inject"

    old = _parse_len(m.group(1))
    if old is None:
        return content, "skip"  # e.g. width="100%": leave alone, don't invent
    new_val = old + delta
    new_tag = svg_tag[: m.start()] + f'{attr}="{new_val:.0f}"' + svg_tag[m.end() :]
    new_content = content[: svg_match.start()] + new_tag + content[svg_match.end() :]
    return new_content, "add"


def _set_root_svg_dim(content: str, attr: str, value: float) -> tuple[str, str]:
    """Set ``attr`` on the root <svg> to an absolute value (no delta).

    Mirrors :func:`_sync_root_svg_dim` but for crop fixes, which set the
    viewBox to an absolute rectangle rather than growing it. Returns
    (new_content, mode) where mode is ``"set"``, ``"inject"``, or ``"skip"``
    (when the existing attr is non-numeric, e.g. ``width="100%"``).

    Handles both quote styles for the existing value; when injecting, the new
    attribute is inserted *inside* the root ``<svg>`` open tag (before its
    closing ``>``), using the SVG's own quote style where discernible.
    """
    svg_match = re.search(r"<svg\b[^>]*>", content)
    if not svg_match:
        return content, "skip"
    svg_tag = svg_match.group(0)

    # Match the attr in EITHER quote style.
    m = re.search(rf'\b{attr}\s*=\s*"([^"]*)"', svg_tag) or re.search(
        rf"\b{attr}\s*=\s*'([^']*)'", svg_tag
    )
    if not m:
        # Inject inside the tag, just before the closing '>'.
        injected = f' {attr}="{value:.0f}"'
        close = svg_tag[-1]  # '>' (or '/>' on a self-closing root — rare)
        new_tag = svg_tag[:-1] + injected + close
        new_content = (
            content[: svg_match.start()] + new_tag + content[svg_match.end() :]
        )
        return new_content, "inject"

    old = _parse_len(m.group(1))
    if old is None:
        return content, "skip"  # e.g. width="100%": leave alone, don't invent
    # Preserve the original quote style on rewrite.
    quote = '"' if m.group(0)[len(attr) + 1] == '"' else "'"
    new_val_str = f"{value:.0f}"
    new_attr = f"{attr}={quote}{new_val_str}{quote}"
    new_tag = svg_tag[: m.start()] + new_attr + svg_tag[m.end() :]
    new_content = content[: svg_match.start()] + new_tag + content[svg_match.end() :]
    return new_content, "set"


def _crop_viewbox(
    content: str, new_x: float, new_y: float, new_w: float, new_h: float
) -> tuple[str, str | None]:
    """Rewrite the viewBox to an absolute crop rectangle (content_misfit fix).

    The inverse of :func:`_expand_viewbox`: instead of growing the canvas by a
    delta, this sets the viewBox to a tight rectangle around the visible
    content, then syncs the root ``<svg>`` width/height to the new viewBox so
    the diagram renders at its natural size instead of being scaled into a
    corner. Handles both quote styles; preserves the original quote style.
    """
    new_content = content
    skipped: list[str] = []

    def replace_vb(quote: str, inner: str) -> str:
        parts = inner.split()
        if len(parts) != 4:
            skipped.append("viewBox (unrecognized format)")
            return f"viewBox={quote}{inner}{quote}"
        try:
            [float(p) for p in parts]  # validate all 4 parse as numbers
        except ValueError:
            skipped.append(f"viewBox={quote}{inner}{quote}")
            return f"viewBox={quote}{inner}{quote}"
        return f"viewBox={quote}{new_x:.0f} {new_y:.0f} {new_w:.0f} {new_h:.0f}{quote}"

    def vb_callback(m: "re.Match[str]") -> str:
        if m.group(1) is not None:
            return replace_vb('"', m.group(1))
        return replace_vb("'", m.group(2))

    new_content = re.sub(
        r'viewBox\s*=\s*"([^"]*)"|viewBox\s*=\s*\'([^\']*)\'',
        vb_callback,
        new_content,
        count=1,
    )

    # Sync the root <svg> width/height to the cropped viewBox dims (absolute
    # set, not a delta) so the canvas shrinks with the content.
    new_content, w_mode = _set_root_svg_dim(new_content, "width", new_w)
    new_content, h_mode = _set_root_svg_dim(new_content, "height", new_h)

    if skipped:
        return new_content, f"viewBox cropped; skipped {', '.join(skipped)}"
    return (
        new_content,
        f"viewBox cropped to {new_w:.0f}x{new_h:.0f} ({w_mode}/{h_mode})",
    )


def _dim_msg(axis: str, delta: float, mode: str) -> str:
    if mode == "add":
        return f"+{delta:.0f}{axis}"
    if mode == "inject":
        return f"{axis} (injected)"
    return f"{axis} (unchanged)"


def _sync_inner_label_px(
    content: str,
    dom_index: int | None,
    attrs: dict,
    expand_w: float,
    expand_h: float,
) -> str:
    """Grow fixed CSS px sizes inside one ``<foreignObject>``.

    draw.io (and similar) size the HTML label with ``width:120px`` copied
    from the foreignObject. Widening only the foreignObject leaves that CSS
    clipping the text. Values within 1.5px of the old foreignObject size are
    updated; other lengths are left alone.
    """
    if dom_index is None:
        return content
    blocks = list(
        re.finditer(r"<foreignObject\b[^>]*>.*?</foreignObject>", content, re.DOTALL)
    )
    if dom_index < 0 or dom_index >= len(blocks):
        return content
    block = blocks[dom_index]
    old_w = _parse_len(str(attrs.get("width", "")))
    old_h = _parse_len(str(attrs.get("height", "")))

    def _bump(text: str, prop: str, old: float | None, delta: float) -> str:
        if old is None or delta <= 0:
            return text
        new = old + delta

        def repl(m: "re.Match[str]") -> str:
            try:
                val = float(m.group(1))
            except ValueError:
                return m.group(0)
            if abs(val - old) > 1.5:
                return m.group(0)
            return f"{prop}:{new:.0f}px"

        return re.sub(
            rf"{prop}\s*:\s*([0-9.]+)\s*px",
            repl,
            text,
            flags=re.IGNORECASE,
        )

    # max-width before width: the shorter pattern also matches the longer name.
    inner = block.group(0)
    inner = _bump(inner, "max-width", old_w, expand_w)
    inner = _bump(inner, "width", old_w, expand_w)
    inner = _bump(inner, "max-height", old_h, expand_h)
    inner = _bump(inner, "height", old_h, expand_h)
    return content[: block.start()] + inner + content[block.end() :]


def _fix_card(
    content: str,
    attrs: dict,
    expand_w: float,
    expand_h: float,
    *,
    dom_index: int | None = None,
    tag: str = "rect",
) -> tuple[str, str | None]:
    """Expand the rect identified by ``attrs`` (x/y/width/height).

    Identifies the target rect by:

      1. ``dom_index`` (preferred when given): the Nth ``<rect>`` in document
         order, matching the checker's ``querySelectorAll('rect')`` indexing.
         This uniquely identifies a rect even when several share identical
         x/y/width/height/fill (e.g. a background layer + a card layer).
      2. A *fingerprint* of several attributes (x, y, width, height, fill)
         parsed as proper ``key="value"`` tokens, not substrings. Used as the
         locator when dom_index is absent, and as a sanity check otherwise.

    Avoids two known mis-fixes:
      * two rects sharing the same x/y used to collapse onto one slot and
        only the first was ever widened;
      * ``x="10"`` substring-matched inside ``transform="translate(10,…)"``.

    Supports both double- and single-quoted attribute values.
    """
    if expand_w <= 0 and expand_h <= 0:
        return content, None

    target_x = attrs.get("x", "")
    target_y = attrs.get("y", "")
    target_w = attrs.get("width", "")
    target_h = attrs.get("height", "")
    target_fill = attrs.get("fill", "")

    # Sanity: we can only rewrite widths we understand.
    cur_w = _parse_len(target_w) if target_w else None
    cur_h = _parse_len(target_h) if target_h else None

    # Fingerprint: every (attr, value) pair here must be present on the rect
    # tag, matched as a real attribute token (so x="10" won't match x="100").
    fingerprint = [
        (k, v)
        for k, v in (
            ("x", target_x),
            ("y", target_y),
            ("width", target_w),
            ("height", target_h),
            ("fill", target_fill),
        )
        if v and v != "0"  # x/y default to "0" when absent; not discriminative
    ]

    def attr_value(tag: str, attr: str) -> str | None:
        """Return the value of ``attr`` in ``tag``, or None if absent.

        Tolerates either quote style and optional whitespace around ``=``.
        """
        m = re.search(rf'\b{attr}\s*=\s*"([^"]*)"', tag)
        if m:
            return m.group(1)
        m = re.search(rf"\b{attr}\s*=\s*'([^']*)'", tag)
        if m:
            return m.group(1)
        return None

    def set_attr(tag: str, attr: str, old_val: str, new_val: str) -> str:
        """Rewrite ``attr``'s value, preserving its quote style."""
        for quote in ('"', "'"):
            pat = f"{attr}={quote}{old_val}{quote}"
            if pat in tag:
                return tag.replace(pat, f"{attr}={quote}{new_val}{quote}", 1)
        return tag  # shouldn't happen — caller verified the value

    # Match a <rect …> open tag (covers both self-closing and open-close forms,
    # since width/height always live on the open tag).
    for rect_n, m in enumerate(re.finditer(rf"<{tag}\b[^>]*>", content)):
        tag = m.group()

        # When we have a dom_index, locate the rect by position (matching the
        # checker's querySelectorAll('rect') order) and use the fingerprint
        # only as a sanity check. Without dom_index, fall back to matching the
        # first rect whose fingerprint matches.
        if dom_index is not None:
            if rect_n != dom_index:
                continue
            # Sanity: the Nth rect should still carry the attrs we expect. If
            # the SVG was hand-edited between check and fix, refuse rather than
            # widen the wrong rect.
            if not all(attr_value(tag, k) == v for k, v in fingerprint):
                return (
                    content,
                    f"rect[{dom_index}] skipped: attrs no longer match "
                    f"(file changed since check?)",
                )
        else:
            if not all(attr_value(tag, k) == v for k, v in fingerprint):
                continue

        new_tag = tag
        parts: list[str] = []

        if expand_w > 0:
            if cur_w is None:
                return (
                    content,
                    f"rect({target_x},{target_y}) skipped: unsupported "
                    f'width="{target_w}"',
                )
            new_w = cur_w + expand_w
            new_tag = set_attr(new_tag, "width", target_w, f"{new_w:.0f}")
            parts.append(f"width {target_w}->{new_w:.0f}")

        if expand_h > 0:
            if cur_h is None:
                return (
                    content,
                    f"rect({target_x},{target_y}) skipped: unsupported "
                    f'height="{target_h}"',
                )
            new_h = cur_h + expand_h
            new_tag = set_attr(new_tag, "height", target_h, f"{new_h:.0f}")
            parts.append(f"height {target_h}->{new_h:.0f}")

        if new_tag == tag:
            return content, None

        new_content = content[: m.start()] + new_tag + content[m.end() :]
        label = (
            f"{tag}[{dom_index}]"
            if dom_index is not None
            else f"{tag}({target_x},{target_y})"
        )
        return new_content, f"{label} {' '.join(parts)}"

    return content, None
