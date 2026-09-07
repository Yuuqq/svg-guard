# Code Review: Yuuqq/svg-guard

## Summary
The codebase is generally well-structured, leveraging Python for orchestration and headless Chromium (Playwright) for accurate DOM/SVG coordinate measurement. The separation of detection (`checker.py`), fixing (`fixer.py`), and reporting (`report.py`) is clean and modular. The approach of using real browser rendering is highly effective for solving CJK text measurement issues where static heuristics often fail.

However, several issues exist across security, data integrity, and reliability that require attention.

---

## CRITICAL

### 1. SSRF and Local File Exfiltration Risk via Playwright
- **Location**: `src/svg_guard/checker.py` (Playwright Page Configuration)
- **Impact**: SVGs are opened in a fully privileged Playwright page using `file://` URLs. If a user runs `svg-guard` on an untrusted or externally provided SVG, malicious scripts (`<script>`) or external resources (`<image href="...">`) will be executed or fetched. An attacker could potentially execute JavaScript in the `file://` origin context, allowing exfiltration of local files via `fetch('file:///etc/passwd')` or Server-Side Request Forgery (SSRF) via auto-loading network assets.
- **Fix Suggestion**: Disable JavaScript execution and block all non-essential network requests when launching the page. SVGs do not need JavaScript or external network requests to render text bounding boxes for static analysis.
  ```python
  # In src/svg_guard/checker.py (BrowserRunner.__enter__)
  self.page = self._browser.new_page(
      viewport={"width": self.config.viewport_w, "height": self.config.viewport_h},
      java_script_enabled=False, # Prevent script execution in SVGs
  )
  # Block all network requests to prevent SSRF
  self.page.route("**/*", lambda route: route.abort())
  ```

### 2. Regex Desynchronization on XML Comments in Fixer
- **Location**: `src/svg_guard/fixer.py` (`_fix_card`, around line 431)
- **Impact**: `_fix_card` finds rects using `re.finditer(r"<rect\b[^>]*>", content)` to align with the JS DOM index (`dom_index`). However, the DOM `querySelectorAll('rect')` ignores `<rect>` tags inside XML comments (e.g., `<!-- <rect x="0" y="0" width="10" height="10" /> -->`), while the regex still matches them. If an SVG contains a commented-out rect before the target rect, `rect_n` in the Python loop will diverge from the JS `dom_index`, causing legitimate fixes to be skipped with `rect[X] skipped: attrs no longer match`.
- **Fix Suggestion**: Using regex to parse XML is inherently fragile. For robust non-destructive updates, either:
  1. Strip XML comments (`<!--.*?-->`) from the SVG content string before running `re.finditer` to ensure index alignment.
  2. Or, if `dom_index` mismatches due to comments, fall back entirely to the attribute fingerprint matching instead of strictly requiring `rect_n == dom_index`.

---

## HIGH

### 1. Silent Data Corruption via `latin-1` Decoding Fallback
- **Location**: `src/svg_guard/_io.py` (`read_svg`, line 42)
- **Impact**: The fallback `raw.decode("latin-1")` ensures no crash occurs when UTF-8 and the XML declared encoding fail. However, `latin-1` will blindly decode *any* byte sequence, silently corrupting characters (e.g., Chinese/Japanese text) that are in an unsupported encoding or wrongly identified. When the corrupted text is written back in `write_text_atomic(..., encoding="utf-8")`, the original file's text gets permanently destroyed (mojibake).
- **Fix Suggestion**: Refuse to process files whose encoding cannot be definitively parsed into correct Unicode. Remove the `latin-1` fallback and allow it to raise a `UnicodeDecodeError`, or catch it and return a sentinel value (or raise a custom exception) so the file can be safely skipped with a clear warning, rather than silently corrupting user data.

---

## MEDIUM

### 1. ReDoS Vulnerability in `_XML_ENC_RE`
- **Location**: `src/svg_guard/_io.py` (`_XML_ENC_RE`, line 18)
- **Impact**: The regex `rb'<\?xml[^?]*encoding=["\']([A-Za-z0-9_\-]+)["\']'` uses the `[^?]*` pattern which can cause catastrophic backtracking if a very long, malformed XML prolog without an encoding attribute is encountered.
- **Fix Suggestion**: Simplify the regex to use a non-greedy match.
  ```python
  _XML_ENC_RE = re.compile(rb'<\?xml.*?encoding=["\']([A-Za-z0-9_\-]+)["\']', re.IGNORECASE)
  ```

---

## LOW

### 1. Missing `conftest.py` for Test Discovery
- **Location**: `tests/`
- **Impact**: Running `pytest` directly in the project root fails because `svg_guard` is not in the `PYTHONPATH` by default unless installed via `pip install -e .`.
- **Fix Suggestion**: Add an empty `tests/conftest.py` or one that modifies `sys.path` to include `src/`, improving the developer experience for out-of-the-box testing.

### 2. File Backup TOCTOU (Time-of-Check to Time-of-Use)
- **Location**: `src/svg_guard/fixer.py` (`_safe_backup`, line 159)
- **Impact**: `_safe_backup` checks `while target.exists():` and then copies the file. This is technically susceptible to a race condition if another process creates the backup file in between the check and the copy. Given this is a local CLI tool, the risk is negligible.
- **Fix Suggestion**: For strict correctness, use `os.open` with `os.O_CREAT | os.O_EXCL` to ensure the backup file is created atomically without races.
