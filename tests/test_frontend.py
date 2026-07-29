"""
test_frontend.py – static checks on the frontend, no browser required.

There is no JS test runner in this project, so the HTML/JS/CSS would otherwise be
completely untested. These assertions are pure text inspection – they run in
milliseconds and still catch three classes of bug that the backend suite is
structurally blind to:

* **Dangling element ids.** app.js resolves every element it needs at load time;
  a missing id makes initialization throw and kills the whole UI. This happened
  once (a missing ``#openNewTabBtn``) while every Python test stayed green. See
  tests/check_frontend_ids.py for the full story – CI also runs that script as a
  standalone step so the breakage gets its own named failure.
* **A re-introduced CDN dependency.** The app is meant to work with no internet
  connection at all; one ``<script src="https://...">`` silently undoes that.
* **A broken security-token handshake.** The backend substitutes a placeholder in
  the HTML at serve time; if the placeholder is renamed away, every API call from
  the page is unauthenticated.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.check_frontend_ids import missing_ids

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


# ─── Element ids referenced by app.js ─────────────────────────────────────────
def test_no_dangling_element_ids():
    """Prove app.js never asks for an element id that index.html does not define.

    Regression: ``#openNewTabBtn`` was removed from the HTML but left in app.js.
    ``getElementById`` returned null, ``setupEventListeners()`` dereferenced it
    during startup, and initialization aborted – upload, compile and preview were
    all dead, with no visible error outside the browser console.
    """
    assert missing_ids() == [], f"app.js references missing ids: {missing_ids()}"


def test_open_new_tab_button_exists():
    """Pin the one id whose absence caused the outage above.

    Not redundant with the cross-reference check: that one only compares the two
    files against each other, so deleting the button from index.html *and* its
    references from app.js would keep it green while the feature quietly vanished.
    This test asserts the element itself is still there.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'id="openNewTabBtn"' in html


# ─── Offline self-containment ─────────────────────────────────────────────────
def test_no_external_cdn_references():
    """Prove the app loads nothing from the network – it must work fully offline.

    LaTeX Studio is a local tool people run on their own machine, sometimes on an
    air-gapped one, and pulling a font or script from a CDN would both break that
    and leak the fact that the app is in use. Monaco is vendored locally for the
    same reason.
    """
    for name in ("index.html", "app.js", "style.css"):
        text = (FRONTEND / name).read_text(encoding="utf-8")
        urls = re.findall(r"https?://[^\s\"')]+", text)
        external = [u for u in urls if "w3.org" not in u]  # SVG xmlns namespace is fine
        assert external == [], f"{name} has external URLs: {external}"


# ─── Security token handshake ─────────────────────────────────────────────────
def test_security_token_placeholder_present():
    """Prove index.html still carries the marker the backend swaps for a real token.

    ``main.py`` serves the page through a plain string replace. If the placeholder
    were renamed or dropped, the replace would silently no-op, the page would ship
    an empty token, and every API call would be rejected – with no error anywhere
    to explain why. This guards the source file; tests/test_api.py guards the other
    half by asserting the placeholder is *gone* from the served response, so
    together they prove the substitution actually runs.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "__STUDIO_TOKEN__" in html  # replaced at serve time by the backend
