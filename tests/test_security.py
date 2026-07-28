"""
test_security.py – regression tests, one per attack class the audit found.

Each test fails if its guard regresses. These are the safety net for the
Phase 0 security fixes; keep them green.

The attacks covered here, in file order:

* **Cross-origin drive-by** – any web page you visit scripting the API that is
  listening on your own machine.
* **Instance token** – a second layer behind the Origin check, so a request that
  presents a token at all must present the right one.
* **Path traversal**, including ``%2e%2e`` / ``%2f`` percent-encoded forms that
  never contain a literal ``..`` in the URL, and NTFS alternate data streams
  (``file.tex:stream``), which slip past a naive "is it inside the folder?"
  check because the stream name is not a path component.
* **Memory-exhaustion upload** – a huge body that must be refused while it is
  still streaming, not after it has been buffered.
* **.latexmkrc RCE** – latexmk executes a ``latexmkrc`` found in the build
  directory as Perl, so smuggling one in via a ZIP is code execution.
* **\\write18 command execution** – a .tex asking LaTeX to run OS commands.

Everything except the last runs without a LaTeX install. The ``@requires_latex``
one is normally skipped, but the nightly CI job sets
``LATEX_STUDIO_REQUIRE_LATEX=1`` so a broken TeX install fails the run instead of
quietly dropping the live shell-escape check (see conftest.py).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SIMPLE_TEX = (FIXTURES_DIR / "simple" / "main.tex").read_text()

requires_latex = pytest.mark.requires_latex


# ─── Shared helpers ──────────────────────────────────────────────────────────

def _upload_simple(client) -> str:
    """Upload the one-file fixture project and return its session id.

    These tests need a real workspace to aim an attack at; going through the
    endpoint means the target looks exactly like a user's own session.
    """
    res = client.post("/api/upload",
                      files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))])
    return res.json()["session_id"]


# ─── Cross-origin drive-by and the per-instance token ────────────────────────

class TestCrossOriginAndToken:
    """The two guards that keep a local, unauthenticated API local."""

    def test_cross_origin_api_refused(self, client):
        """A request claiming a foreign Origin is refused (drive-by defence).

        The attack: you visit evil.example, its JavaScript posts to
        http://127.0.0.1:8000/api/… and reads your documents. Browsers always
        attach ``Origin`` on cross-origin requests, so an Origin that is present
        and not loopback cannot be this app's own page.

        Regression: the API once sent ``Access-Control-Allow-Origin: *`` together
        with credentials, which invites exactly that.
        """
        res = client.get("/api/status", headers={"Origin": "http://evil.example"})
        assert res.status_code == 403

    def test_localhost_origin_allowed(self, client):
        """The app's own page still works – the guard is not simply "block all".

        Without this test, tightening the Origin check into a blanket refusal
        would look like a security win while breaking the UI for every user.
        """
        res = client.get("/api/status", headers={"Origin": "http://127.0.0.1:8000"})
        assert res.status_code == 200

    def test_no_origin_allowed(self, client):
        """Non-browser clients (no Origin) are allowed – the loopback bind is
        their boundary.

        curl, the launcher's health check and this suite send no Origin. They
        are not the threat model: reaching the socket at all already requires
        code running on the machine.
        """
        assert client.get("/api/status").status_code == 200

    def test_wrong_token_refused(self, client):
        """A request presenting the wrong ``X-Studio-Token`` is refused.

        The token is minted per server start and injected into index.html, so
        only a script served from this origin can read it. This is the layer
        that still holds if something ever reaches the API without an Origin
        header the middleware can judge.
        """
        res = client.get("/api/status", headers={"X-Studio-Token": "definitely-wrong"})
        assert res.status_code == 403

    def test_correct_token_allowed(self, client):
        """The token the app actually minted is accepted.

        Compared against ``app.state.token`` rather than a hard-coded value,
        which is what proves the frontend and the middleware agree on the same
        secret – if they drifted, the real UI would 403 itself on every call.
        """
        token = client.app.state.token
        res = client.get("/api/status", headers={"X-Studio-Token": token})
        assert res.status_code == 200


# ─── Path traversal, encoded traversal and NTFS data streams ─────────────────

class TestPathTraversal:
    """Nothing may address a file outside the directory it belongs to."""

    def test_static_route_traversal_blocked(self, client):
        """Regression (C5): the catch-all static route must not read outside
        the frontend directory.

        ``GET /{filename:path}`` serves the UI's assets. Because the server
        percent-decodes before routing, ``%2e%2e`` arrives as ``..`` without a
        literal ``..`` ever appearing in the request line – so a filter that
        greps the raw URL for dots is not a defence. The fix resolves the path
        and requires it to stay under the frontend root.
        """
        for attempt in (
            "/%2e%2e/backend/config.py",
            "/../backend/config.py",
            "/%2e%2e%2f%2e%2e%2fbackend%2fconfig.py",
        ):
            res = client.get(attempt)
            assert res.status_code in (400, 404)
            # Status alone is not enough: a 200-with-wrong-body regression would
            # pass the check above, so assert the file's content never leaks.
            assert "ALLOW_SHELL_ESCAPE" not in res.text  # config.py content must not leak

    def test_file_api_encoded_traversal_blocked(self, client):
        """Reading through the editor API cannot escape the session workspace.

        ``..%2f..%2f`` is the encoded form; the path is decoded before it
        reaches the handler, so the guard has to work on the resolved path, not
        on the string the client sent.
        """
        session_id = _upload_simple(client)
        res = client.get(f"/api/files/{session_id}/..%2f..%2fbackend%2fmain.py")
        assert res.status_code == 400

    def test_file_api_write_traversal_blocked(self, client):
        """Writing through the editor API cannot escape the session workspace.

        Worse than the read case: a write that lands outside the workspace
        plants a file on the machine – drop one where the compiler or the
        launcher will pick it up and the leak becomes execution.
        """
        session_id = _upload_simple(client)
        res = client.put(f"/api/files/{session_id}/..%2f..%2fevil.py", content=b"x")
        assert res.status_code == 400

    def test_ads_colon_rejected(self, client):
        """An NTFS alternate-data-stream path is rejected outright.

        On Windows ``main.tex:stream`` addresses a hidden stream attached to
        main.tex – content that no directory listing shows. It also survives a
        naive "does the resolved path stay inside the session?" check, because
        the stream name is not a path component, which is why ``:`` is banned in
        the path rather than merely normalised. The same rule blocks
        drive-qualified paths such as ``C:\\Windows\\…``.
        """
        session_id = _upload_simple(client)
        # An NTFS alternate-data-stream path contains ':' and must be rejected.
        res = client.get(f"/api/files/{session_id}/main.tex:stream")
        assert res.status_code == 400


# ─── Upload limits: memory exhaustion and the .latexmkrc RCE ─────────────────

class TestUploadLimits:
    """Size caps and the ZIP type whitelist."""

    def test_upload_size_cap(self, client, monkeypatch):
        """An oversized upload is refused with 413 while it is still streaming.

        Patching the cap down to 50 bytes is what makes this testable without
        allocating hundreds of megabytes. The patch targets ``file_manager``
        because that module does ``from backend.config import
        MAX_UPLOAD_SIZE_BYTES`` – the value is copied into its namespace at
        import time, so patching ``backend.config`` would have no effect on the
        running check.
        """
        import backend.file_manager as fm
        monkeypatch.setattr(fm, "MAX_UPLOAD_SIZE_BYTES", 50)
        res = client.post("/api/upload",
                          files=[("files", ("big.tex", b"x" * 500, "text/plain"))])
        assert res.status_code == 413

    def test_put_size_cap(self, client, monkeypatch):
        """The editor's save path is capped too, not just the upload form.

        A PUT is the other way bytes enter the workspace; leaving it uncapped
        would make the upload limit trivially bypassable. The initial upload is
        kept under the patched cap on purpose, so the 413 can only come from the
        PUT.
        """
        import backend.file_manager as fm
        monkeypatch.setattr(fm, "MAX_UPLOAD_SIZE_BYTES", 50)
        session_id = client.post(
            "/api/upload",
            files=[("files", ("main.tex", b"tiny", "text/plain"))]).json()["session_id"]
        res = client.put(f"/api/files/{session_id}/main.tex", content=b"y" * 500)
        assert res.status_code == 413

    def test_zip_extensionless_dropped(self, client):
        """Regression (C9): a ZIP cannot smuggle an extensionless config file in.

        latexmk reads a ``latexmkrc`` found in the build directory and executes
        it as Perl, so this member is remote code execution at the moment the
        user clicks Compile. ZIP extraction once accepted files with no
        extension, which let it through. The name has no leading dot because the
        sanitiser strips those anyway – it is the extension whitelist that has
        to catch this.

        The legitimate .tex must still arrive: bad members are skipped
        individually, so one stray file does not fail a whole project upload.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("main.tex", SIMPLE_TEX)
            archive.writestr("latexmkrc", "system('calc');")
        res = client.post("/api/upload",
                          files=[("files", ("p.zip", buffer.getvalue(), "application/zip"))])
        names = [f["name"] for f in res.json()["files"]]
        assert "main.tex" in names and "latexmkrc" not in names


# ─── Shell escape: \write18 command execution ────────────────────────────────

class TestShellEscape:
    """The headline finding: an uploaded document must not run programs."""

    @requires_latex
    def test_write18_blocked_by_default(self):
        """Regression (C1): \\write18 must NOT run a command by default, and the
        document should still compile.

        ``\\write18`` hands a string to the OS shell, so with shell-escape on,
        any .tex a user opens can do anything that user can. It was once enabled
        unconditionally, despite the docs claiming otherwise.

        This drives ``compile_project`` directly instead of the API, so it pins
        the default down in the compiler itself – the layer that builds the
        command line – rather than merely proving the route does not ask for it.
        The absent ``pwned.txt`` is the proof: the document is expected to
        compile fine, it just must not have run anything.
        """
        from backend.compiler import compile_project
        from backend.file_manager import create_session, delete_session, get_session_dir
        session_id = create_session()
        session_dir = get_session_dir(session_id)
        (session_dir / "main.tex").write_text(
            "\\documentclass{article}\\begin{document}"
            "\\immediate\\write18{echo pwned > pwned.txt}Hello\\end{document}")
        try:
            compile_project(session_dir, "main.tex")
            assert not (session_dir / "pwned.txt").exists(), \
                "shell-escape must be disabled by default"
        finally:
            delete_session(session_id)
