"""Auto-fix engine — repairs overflow issues using regex-based SVG patching."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

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
    content = svg_path.read_text(encoding="utf-8")
    changes: list[str] = []

    if backup and not dry_run:
        _safe_backup(svg_path)

    # Fix viewBox overflow first (changes the canvas, not individual elements).
    # Both rect_viewbox and text_viewbox (orphan text) widen the canvas, so we
    # accumulate their deltas and apply them together to avoid repeated edits.
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
        vb_dw += dw
        vb_dh += dh

    if vb_dw > 0 or vb_dh > 0:
        content, change = _expand_viewbox(content, vb_dw, vb_dh)
        if change:
            changes.append(change)

    # Fix card text overflow (widens/expands rects). Multiple issues can hit
    # the same rect; collapse them by parent identity, taking the max delta in
    # each direction so nothing is silently under-fixed (and so the second
    # issue isn't left looking for an already-rewritten width="...").
    grouped: dict[tuple[str, str], dict[str, float | dict]] = {}
    order: list[tuple[str, str]] = []
    for issue in issues:
        if issue.type != "text_rect":
            continue
        attrs = issue.parent.get("attrs", {})
        key = (attrs.get("x", ""), attrs.get("y", ""))
        slot = grouped.setdefault(
            key, {"expand_w": 0.0, "expand_h": 0.0, "attrs": attrs}
        )
        if key not in order:
            order.append(key)
        slot["expand_w"] = max(slot["expand_w"], issue.fix.get("expand_w", 0) or 0)
        slot["expand_h"] = max(slot["expand_h"], issue.fix.get("expand_h", 0) or 0)

    for key in order:
        slot = grouped[key]
        content, change = _fix_card(
            content, slot["attrs"], slot["expand_w"], slot["expand_h"]
        )
        if change:
            changes.append(change)

    if changes and not dry_run:
        svg_path.write_text(content, encoding="utf-8")

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


def _expand_viewbox(
    content: str, delta_w: float, delta_h: float
) -> tuple[str, str | None]:
    """Widen the viewBox and the root <svg> width/height by the given deltas."""
    if delta_w <= 0 and delta_h <= 0:
        return content, None

    new_content = content
    skipped: list[str] = []

    def replace_vb(m: re.Match) -> str:
        parts = m.group(1).split()
        if len(parts) != 4:
            skipped.append("viewBox (unrecognized format)")
            return m.group(0)
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            skipped.append(f'viewBox="{m.group(1)}"')
            return m.group(0)
        w = nums[2] + delta_w
        h = nums[3] + delta_h
        return f'viewBox="{nums[0]:.0f} {nums[1]:.0f} {w:.0f} {h:.0f}"'

    new_content = re.sub(r'viewBox="([^"]*)"', replace_vb, new_content, count=1)

    parts_msg = []
    if delta_w > 0:
        new_content, ok = _replace_root_svg_attr(new_content, "width", delta_w)
        parts_msg.append(f"+{delta_w:.0f}w" if ok else "width (skipped)")
    if delta_h > 0:
        new_content, ok = _replace_root_svg_attr(new_content, "height", delta_h)
        parts_msg.append(f"+{delta_h:.0f}h" if ok else "height (skipped)")

    msg = f"viewBox expanded ({', '.join(parts_msg)})"
    if skipped:
        msg += f"; skipped {', '.join(skipped)}"
    return new_content, msg


def _fix_card(
    content: str,
    attrs: dict,
    expand_w: float,
    expand_h: float,
) -> tuple[str, str | None]:
    """Expand the rect identified by ``attrs`` (x/y/width/height)."""
    if expand_w <= 0 and expand_h <= 0:
        return content, None

    target_x = attrs.get("x", "")
    target_y = attrs.get("y", "")
    target_w = attrs.get("width", "")
    target_h = attrs.get("height", "")

    # Sanity: we can only rewrite widths we understand.
    cur_w = _parse_len(target_w) if target_w else None
    cur_h = _parse_len(target_h) if target_h else None

    # Build the attribute fingerprints we must match to identify the rect.
    fingerprints = [(k, v) for k, v in (("x", target_x), ("y", target_y)) if v]

    # Match a <rect …> open tag (covers both self-closing and open-close forms,
    # since width/height always live on the open tag).
    for m in re.finditer(r"<rect\b[^>]*>", content):
        tag = m.group()
        if not all(f'{k}="{v}"' in tag for k, v in fingerprints):
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
            new_tag = new_tag.replace(f'width="{target_w}"', f'width="{new_w:.0f}"')
            parts.append(f"width {target_w}->{new_w:.0f}")

        if expand_h > 0:
            if cur_h is None:
                return (
                    content,
                    f"rect({target_x},{target_y}) skipped: unsupported "
                    f'height="{target_h}"',
                )
            new_h = cur_h + expand_h
            new_tag = new_tag.replace(f'height="{target_h}"', f'height="{new_h:.0f}"')
            parts.append(f"height {target_h}->{new_h:.0f}")

        if new_tag == tag:
            return content, None

        new_content = content[: m.start()] + new_tag + content[m.end() :]
        return new_content, f"rect({target_x},{target_y}) {' '.join(parts)}"

    return content, None


def _replace_root_svg_attr(content: str, attr: str, delta: float) -> tuple[str, bool]:
    """Add ``delta`` to ``attr`` on the root <svg> tag only.

    Returns the new content and a flag indicating whether the attribute was
    actually rewritten (False when it was missing or non-numeric).
    """
    svg_match = re.search(r"<svg\b[^>]*>", content)
    if not svg_match:
        return content, False
    svg_tag = svg_match.group(0)

    attr_re = re.compile(rf'\b{attr}="([^"]*)"')
    m = attr_re.search(svg_tag)
    if not m:
        return content, False  # leave it alone rather than inventing a value

    old = _parse_len(m.group(1))
    if old is None:
        return content, False

    new_tag = svg_tag[: m.start()] + f'{attr}="{old + delta:.0f}"' + svg_tag[m.end() :]
    return content[: svg_match.start()] + new_tag + content[svg_match.end() :], True
