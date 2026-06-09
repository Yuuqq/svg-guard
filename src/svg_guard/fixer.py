"""Auto-fix engine — repairs overflow issues using regex-based SVG patching."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .checker import Issue


def fix_svg(
    svg_path: Path | str,
    issues: list[Issue],
    *,
    backup: bool = True,
    dry_run: bool = False,
) -> list[str]:
    """Auto-fix overflow issues in an SVG file.

    Returns a list of human-readable change descriptions.
    Creates a .bak backup by default.
    """
    svg_path = Path(svg_path)
    content = svg_path.read_text(encoding="utf-8")
    changes: list[str] = []

    if backup and not dry_run:
        shutil.copy2(svg_path, svg_path.with_suffix(".svg.bak"))

    # Fix viewBox overflow first (changes canvas, not elements)
    for issue in issues:
        if issue.type == "rect_viewbox":
            content, change = _fix_viewbox(content, issue)
            if change:
                changes.append(change)

    # Fix card text overflow (widens/expands rects)
    for issue in issues:
        if issue.type == "text_rect":
            content, change = _fix_card(content, issue)
            if change:
                changes.append(change)

    if changes and not dry_run:
        svg_path.write_text(content, encoding="utf-8")

    return changes


def _fix_viewbox(content: str, issue: Issue) -> tuple[str, str | None]:
    expand_w = issue.fix.get("expand_viewbox_w", 0)
    expand_h = issue.fix.get("expand_viewbox_h", 0)
    if expand_w <= 0 and expand_h <= 0:
        return content, None

    def replace_vb(m: re.Match) -> str:
        parts = m.group(1).split()
        w = float(parts[2]) + expand_w
        h = float(parts[3]) + expand_h
        return f'viewBox="{parts[0]} {parts[1]} {w:.0f} {h:.0f}"'

    new_content = re.sub(r'viewBox="([^"]*)"', replace_vb, content, count=1)

    if expand_w > 0:
        new_content = _replace_first_attr(new_content, "width", expand_w)
    if expand_h > 0:
        new_content = _replace_first_attr(new_content, "height", expand_h)

    return new_content, f"viewBox expanded by +{expand_w}w +{expand_h}h"


def _fix_card(content: str, issue: Issue) -> tuple[str, str | None]:
    expand_w = issue.fix.get("expand_w", 0)
    expand_h = issue.fix.get("expand_h", 0)
    if expand_w <= 0 and expand_h <= 0:
        return content, None

    attrs = issue.parent.get("attrs", {})
    target_x = attrs.get("x", "")
    target_y = attrs.get("y", "")
    target_w = attrs.get("width", "")
    target_h = attrs.get("height", "")

    for m in re.finditer(r"<rect\b[^>]*/>", content):
        tag = m.group()
        if not all(
            f'{k}="{v}"' in tag
            for k, v in [("x", target_x), ("y", target_y),
                         ("width", target_w), ("height", target_h)]
            if v
        ):
            continue

        new_tag = tag
        if expand_w > 0 and target_w:
            new_w = float(target_w) + expand_w
            new_tag = new_tag.replace(f'width="{target_w}"', f'width="{new_w:.0f}"')
        if expand_h > 0 and target_h:
            new_h = float(target_h) + expand_h
            new_tag = new_tag.replace(f'height="{target_h}"', f'height="{new_h:.0f}"')

        new_content = content[: m.start()] + new_tag + content[m.end() :]
        parts = []
        if expand_w > 0:
            parts.append(f"width {target_w}->{float(target_w) + expand_w:.0f}")
        if expand_h > 0:
            parts.append(f"height {target_h}->{float(target_h) + expand_h:.0f}")
        return new_content, f'rect({target_x},{target_y}) {" ".join(parts)}'

    return content, None


def _replace_first_attr(content: str, attr: str, delta: float) -> str:
    def replacer(m: re.Match) -> str:
        old_val = float(m.group(1))
        return f'{attr}="{old_val + delta:.0f}"'

    return re.sub(rf'{attr}="([^"]*)"', replacer, content, count=1)
