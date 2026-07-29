"""
check_frontend_ids.py – catch the "getElementById returns null" class of bug.

Cross-references ``frontend/index.html`` against ``frontend/app.js`` and fails if
app.js references an element id that the HTML does not define.

WHY THIS EXISTS. app.js caches every element it will ever touch in a single
``dom = {...}`` object built at load time, via ``$ = (id) => document.getElementById(id)``.
A missing id does not throw there – ``$()`` simply yields ``null`` – so the failure
surfaces later and much louder: ``setupEventListeners()`` runs during startup and
does ``dom.openNewTabBtn.addEventListener(...)``, which on ``null`` throws, aborts
initialization, and leaves the *entire* UI inert with nothing but a console error.
That is not hypothetical – a missing ``#openNewTabBtn`` did exactly this. No Python
test touches the DOM, so the backend suite stayed green throughout.

This script is the cheap insurance: no browser, no JS runtime, milliseconds. CI
runs it as its own step before pytest so this specific breakage gets its own
clearly-named red mark instead of hiding inside a test summary.

The check is deliberately one-directional. Ids that exist in the HTML but are never
named in app.js are perfectly fine – ``<label for=...>`` and ``aria-labelledby`` /
``aria-describedby`` point at ids too – so only ``js_ids - html_ids`` is a failure.

Runnable directly (``python tests/check_frontend_ids.py`` – what CI does) and also
exercised from tests/test_frontend.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
# Resolved from __file__ rather than the CWD so the script behaves the same when
# CI invokes it as `python tests/check_frontend_ids.py` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "frontend" / "index.html"
JS = ROOT / "frontend" / "app.js"


# ─── The check ────────────────────────────────────────────────────────────────
def missing_ids() -> list[str]:
    """Return ids referenced in app.js that are absent from index.html.

    Both sides are scraped with regexes instead of being parsed: a real HTML/JS
    parser would mean a third-party dependency for a check whose whole selling
    point is that it is free to run. The frontend is hand-written with one
    consistent quoting style, so patterns are sufficient.

    Two JS reference forms are recognised – the ``$('id')`` shorthand that builds
    the ``dom`` cache, and any direct ``getElementById(...)`` call. An empty
    result means every id app.js can ask for actually exists.
    """
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))  # index.html quotes attributes with "
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js)) | set(  # app.js quotes strings with '
        re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js)
    )
    return sorted(js_ids - html_ids)


# ─── CLI entry point ──────────────────────────────────────────────────────────
def main() -> int:
    """Print a human-readable verdict and return a process exit code.

    Kept separate from ``missing_ids()`` so CI gets the offending ids printed one
    per line in its log, while the pytest wrapper can still assert on the plain
    list. Returns 0 when clean, 1 when at least one id is missing.
    """
    missing = missing_ids()
    if missing:
        print("FAIL: app.js references ids not present in index.html:")
        for missing_id in missing:
            print(f"  - {missing_id}")
        return 1
    print("OK: every id referenced in app.js exists in index.html.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
