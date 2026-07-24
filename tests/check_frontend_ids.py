"""
check_frontend_ids.py – catch the "getElementById returns null" class of bug.

Parses frontend/index.html and frontend/app.js and fails if app.js references an
element id that does not exist in the HTML. This is exactly the defect that used
to break the app on load (a missing #openNewTabBtn aborted initialization).

Runnable directly (``python tests/check_frontend_ids.py`` – used by CI) and also
exercised by tests/test_frontend.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "frontend" / "index.html"
JS = ROOT / "frontend" / "app.js"


def missing_ids() -> list[str]:
    """Return ids referenced in app.js that are absent from index.html."""
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js)) | set(
        re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js)
    )
    return sorted(js_ids - html_ids)


def main() -> int:
    missing = missing_ids()
    if missing:
        print("FAIL: app.js references ids not present in index.html:")
        for m in missing:
            print(f"  - {m}")
        return 1
    print("OK: every id referenced in app.js exists in index.html.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
