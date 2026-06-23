# svg-guard

Detect and auto-fix text overflow in SVG diagrams.

When SVG diagrams use hardcoded absolute coordinates — common in technical documentation, textbooks, and infographics — text frequently overflows its container boxes. This is especially painful with CJK characters, where rendered width varies by font and platform. **svg-guard** renders each SVG in a real browser, measures every `<text>` element against its parent `<rect>`, and reports (or fixes) any overflow.

## Features

- **Accurate detection** — uses Chromium via Playwright for real rendering measurements, not heuristic estimates
- **Two-phase check** — catches both text→rect overflow *and* rect→viewBox overflow
- **Auto-fix** — widens cards and expands viewBox to resolve overflow automatically
- **HTML report** — generates a self-contained visual report of all issues
- **CI-friendly** — exits with code 1 on issues; supports JSON and HTML output
- **Backup-safe** — creates `.bak` files before any fix

## Install

```bash
pip install svg-guard
playwright install chromium
```

Requires Python 3.10+.

## Quick Start

```bash
# Check all SVGs in current directory
svg-guard check --dir ./images --verbose

# Check and generate HTML + JSON reports
svg-guard check --dir ./images --json report.json --html report.html

# Auto-fix detected issues
svg-guard fix --dir ./images

# Preview fixes without writing
svg-guard fix --dir ./images --dry-run
```

## CLI Reference

### `svg-guard check`

Detect overflow issues in SVG files.

| Flag | Default | Description |
|------|---------|-------------|
| `--dir` | `.` | Directory containing SVG files |
| `--verbose`, `-v` | off | Show per-file details |
| `--json FILE` | — | Write JSON report |
| `--html FILE` | — | Write HTML visual report |

Exit code: **0** if no issues, **1** if any overflow detected.

### `svg-guard fix`

Auto-fix detected overflow issues.

| Flag | Default | Description |
|------|---------|-------------|
| `--dir` | `.` | Directory containing SVG files |
| `--dry-run` | off | Show what would change without modifying files |
| `--no-backup` | off | Skip creating `.svg.bak` backup files |

### `svg-guard report`

Check every SVG in the directory and render the results as a self-contained
HTML report (runs the full check, then writes the report).

| Flag | Default | Description |
|------|---------|-------------|
| `--dir` | `.` | Directory containing SVG files |
| `--output`, `-o` | `svg-guard-report.html` | Output HTML path |

## Programmatic API

```python
from pathlib import Path
from svg_guard import BrowserRunner, DetectionConfig, check_svg, fix_svg

# Check a single file, reusing one browser for many operations
with BrowserRunner() as runner:
    result = check_svg(runner.page, Path("diagram.svg"))
    print(f"{'OK' if result.ok else 'ISSUES'}: {len(result.issues)} found")

    # Auto-fix
    if not result.ok:
        changes = fix_svg(Path("diagram.svg"), result.issues)
        for change in changes:
            print(f"  Fixed: {change}")

# Batch check a directory — pass the same runner to skip re-launching Chromium
from svg_guard import check_directory
with BrowserRunner() as runner:
    for d in ("./images", "./icons"):
        results, total = check_directory(d, runner=runner)
```

`BrowserRunner` accepts a `DetectionConfig` to tune thresholds and viewport:

```python
cfg = DetectionConfig(pad=1.0, viewport_w=2000)  # stricter, wider canvas
with BrowserRunner(cfg) as runner:
    ...
```

**Library use is silent by default.** Progress output goes through Python's
`logging` (logger name `"svg_guard"`); the CLI attaches a handler so users
see progress, but `import svg_guard` alone prints nothing. Add a handler if
you want logs in your own tool.

## How It Works

1. **Render** — Each SVG is loaded into a headless Chromium page via Playwright
2. **Measure** — `getBBox()` + `getCTM()` give each element's bounds in the SVG's own user units (viewBox space), correctly accumulating transforms like `rotate` and nested `<svg>`. This is viewport-independent: results don't shift when the browser window is resized.
3. **Associate** — Each text element is matched to its nearest parent rect by center-point containment
4. **Detect** — Two phases:
   - **Phase 1**: text extends beyond its parent rect → `text_rect` issue
   - **Phase 2**: rect extends beyond the SVG viewBox → `rect_viewbox` issue
5. **Fix** — For each issue, the SVG source is patched (preserving formatting):
   - ViewBox overflow → increases `viewBox` dimensions (and the root `<svg>` width/height)
   - Card overflow → increases the rect's `width`/`height` attributes

### Tuning detection

All thresholds live in `DetectionConfig` (in user units, not CSS pixels) and can be passed to `check_svg` / `check_directory`:

```python
from svg_guard import DetectionConfig, check_svg

# Stricter: flag text that's even slightly snug, consider small rects too
cfg = DetectionConfig(pad=1.0, min_rect_w=20.0, min_rect_h=20.0)
result = check_svg(page, Path("diagram.svg"), config=cfg)
```

### Limitations

- **Left/top text overflow is reported but not auto-fixed.** Widening a rect grows it toward the bottom-right, so it can never cover text that starts *before* the rect's left/top edge; auto-fixing would loop forever re-expanding. Such issues are flagged `fixable=false` and the fixer skips them with a clear message — move the text manually.
- **Rotated elements** are measured by their axis-aligned bounding box in viewBox space (transforms are correctly accumulated via `getCTM`). Exact rotated-region containment (a rotated rect with text rotated differently) is approximated, not polygon-precise.
- **CJK fonts in CI**: headless Chromium on a Linux runner has no CJK fonts by default, so Chinese/Japanese text may measure differently than on your machine. Install a CJK font (`fonts-noto-cjk` on Debian/Ubuntu) in CI for consistent results.

## Why Not Heuristics?

SVG text rendering depends on the actual font, kerning, ligatures, and CSS. A 16px Chinese character might render as 14px or 18px depending on the font. The only reliable way to detect overflow is to render and measure — which is exactly what svg-guard does.

## Development

```bash
git clone https://github.com/Yuuqq/svg-guard.git
cd svg-guard
pip install -e ".[dev]"
playwright install chromium
pytest -v
```

## License

MIT
