"""
test_frontend.py – static checks on the frontend.

These are cheap and catch two whole classes of bug without a browser:
  * a JS reference to a non-existent element id (broke the app on load before);
  * a re-introduced external CDN dependency (breaks offline use).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.check_frontend_ids import missing_ids

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def test_no_dangling_element_ids():
    assert missing_ids() == [], f"app.js references missing ids: {missing_ids()}"


def test_open_new_tab_button_exists():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'id="openNewTabBtn"' in html


def test_no_external_cdn_references():
    """The app must be fully offline-capable – no external hosts."""
    for name in ("index.html", "app.js", "style.css"):
        text = (FRONTEND / name).read_text(encoding="utf-8")
        urls = re.findall(r"https?://[^\s\"')]+", text)
        external = [u for u in urls if "w3.org" not in u]  # SVG xmlns namespace is fine
        assert external == [], f"{name} has external URLs: {external}"


def test_security_token_placeholder_present():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "__STUDIO_TOKEN__" in html  # replaced at serve time by the backend
