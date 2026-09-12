# CJK Demo Result

The `svg-guard` tool successfully detected `text_rect` overflow in the flowchart and applied fixes.

The report artifacts are found in `out/`.
The original overflowing SVG remains intact, while the tool's automatically corrected version (which widened the `<rect>` tags from width 180 to 299 to accommodate the CJK text) has been saved as `flowchart_overflow.fixed.svg`.
