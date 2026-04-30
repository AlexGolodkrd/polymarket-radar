"""Phase 9ggg (29.04.2026) — JS sanity check for dashboard.html.

Extracts inline <script> blocks from Scripts/dashboard.html and runs
them through Node.js's `new Function()` parser to catch SyntaxError
before commit/deploy.

Why this exists: we shipped a broken UI TWICE this week:
  1. nearBufferLabel null reference -> null.textContent crash
  2. const deals declared in two scopes -> SyntaxError -> ALL tabs dead

Usage:
  python scripts/lint_dashboard_js.py
  -> exit 0 if JS parses, exit 1 with error message otherwise.
"""
import re
import subprocess
import sys
import tempfile
import os

DASHBOARD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'Scripts', 'dashboard.html')


def extract_inline_js(html_path: str) -> str:
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    pattern = re.compile(
        r'<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    blocks = pattern.findall(html)
    return '\n;// ---- next block ----\n'.join(blocks) if blocks else ''


def lint_js(js_source: str) -> tuple:
    if not js_source.strip():
        return True, "no inline JS"
    with tempfile.NamedTemporaryFile(
            mode='w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(js_source)
        tmp_path = f.name
    try:
        check_script = (
            "try {"
            "  const fs = require('fs');"
            f"  const src = fs.readFileSync({tmp_path!r}, 'utf8');"
            "  new Function(src);"
            "  console.log('OK');"
            "} catch (e) {"
            "  console.error('JS_SYNTAX_ERROR:', e.message);"
            "  process.exit(1);"
            "}"
        )
        result = subprocess.run(
            ['node', '-e', check_script],
            capture_output=True, text=True, timeout=10,
            encoding='utf-8',
        )
        if result.returncode == 0:
            return True, "JS parse OK"
        return False, (result.stderr or result.stdout).strip()
    except FileNotFoundError:
        return True, "node not installed (skipping)"
    except subprocess.TimeoutExpired:
        return False, "node parser timed out"
    finally:
        try: os.unlink(tmp_path)
        except Exception: pass


def main():
    if not os.path.exists(DASHBOARD):
        print(f"[lint] dashboard.html not found at {DASHBOARD}")
        return 0
    js = extract_inline_js(DASHBOARD)
    if not js.strip():
        print("[lint] no inline JS")
        return 0
    ok, msg = lint_js(js)
    if ok:
        print(f"[lint] {msg} ({len(js)} chars)")
        return 0
    print(f"[lint] FAIL: {msg}")
    if os.environ.get('ALLOW_BROKEN_JS') == '1':
        print("[lint] ALLOW_BROKEN_JS=1 - bypassing")
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
