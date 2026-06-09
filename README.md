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

Generate an HTML report without checking.

| Flag | Default | Description |
|------|---------|-------------|
| `--dir` | `.` | Directory containing SVG files |
| `--output`, `-o` | `svg-guard-report.html` | Output HTML path |

## Programmatic API

```python
from pathlib import Path
from playwright.sync_api import sync_playwright
from svg_guard import check_svg, check_directory, fix_svg

# Check a single file
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1200})

    result = check_svg(page, Path("diagram.svg"))
    print(f"{'OK' if result.ok else 'ISSUES'}: {len(result.issues)} found")

    # Auto-fix
    if not result.ok:
        changes = fix_svg(Path("diagram.svg"), result.issues)
        for change in changes:
            print(f"  Fixed: {change}")

    browser.close()

# Batch check a directory
results, total = check_directory("./images", verbose=True, json_out="report.json")
```

## How It Works

1. **Render** — Each SVG is loaded into a headless Chromium page via Playwright
2. **Measure** — `getBoundingClientRect()` gives exact rendered bounds for every `<text>` and `<rect>` element
3. **Associate** — Each text element is matched to its nearest parent rect by center-point containment
4. **Detect** — Two phases:
   - **Phase 1**: text extends beyond its parent rect → `text_rect` issue
   - **Phase 2**: rect extends beyond the SVG viewBox → `rect_viewbox` issue
5. **Fix** — For each issue, the SVG source is patched using regex (preserves formatting):
   - ViewBox overflow → increases `viewBox` dimensions
   - Card overflow → increases the rect's `width`/`height` attributes

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
