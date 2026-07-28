"""
test_api.py – FastAPI endpoint integration tests.

Uses the ``client`` fixture (a TestClient entered as a context manager, so the
app lifespan runs) and the autouse ``isolated_uploads`` fixture, both from
conftest.py. No real server or LaTeX install is required except where marked.

Two conventions these tests pin down, because the frontend branches on them:

* **400 vs 404 for session ids.** A malformed id (not a UUID-v4) answers 400,
  a well-formed id with no workspace answers 404. The UI treats them
  differently – 404 is "your session expired, upload again" – so collapsing the
  two would look like a passing refactor and break the app.
* **Response field names.** The assertions spell out the JSON keys app.js reads
  (``session_id``, ``pdf_url``, ``has_errors``, …). Renaming one server-side
  fails here rather than silently in the browser.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SIMPLE_TEX = (FIXTURES_DIR / "simple" / "main.tex").read_text()

requires_latex = pytest.mark.requires_latex


# ─── Shared helpers ──────────────────────────────────────────────────────────

def _upload_simple(client) -> str:
    """Upload the one-file fixture project and return its session id.

    Most tests just need *a* session to act on. Creating it through the real
    endpoint rather than calling ``create_session()`` keeps them honest: if
    upload breaks, the tests that depend on it fail too instead of exercising a
    workspace the API could never have produced.
    """
    res = client.post("/api/upload",
                      files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))])
    assert res.status_code == 200
    return res.json()["session_id"]


def _make_zip(files: dict) -> bytes:
    """Build a ZIP from ``{name: content}`` entirely in memory (no temp files)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# ─── GET /api/status ─────────────────────────────────────────────────────────

class TestStatus:
    """What the UI reads on load to decide what it can offer the user."""

    def test_status_fields(self, client):
        """Every field the frontend reads is present, and shell-escape reads False.

        The "LaTeX not installed" banner and the compile button are driven by
        these fields, so a missing key silently disables part of the UI rather
        than raising anything.
        """
        data = client.get("/api/status").json()
        for key in ("latex_available", "tools", "default_engine",
                    "shell_escape_enabled", "version"):
            assert key in data
        # Safe default must be reported: shell-escape (arbitrary command
        # execution from a .tex) stays off unless LATEX_ALLOW_SHELL_ESCAPE=1.
        assert data["shell_escape_enabled"] is False


# ─── POST /api/upload ────────────────────────────────────────────────────────

class TestUpload:
    """The only route by which files enter a session workspace."""

    def test_single_tex(self, client):
        """A lone .tex creates a UUID-named session and is detected as the main file.

        Parsing the id with ``uuid.UUID`` is the assertion that matters: the id
        doubles as the workspace directory name and is re-validated by regex on
        every later request, so a non-UUID id would be unusable, not just ugly.
        """
        res = client.post("/api/upload",
                          files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))])
        assert res.status_code == 200
        data = res.json()
        assert uuid.UUID(data["session_id"])          # a real UUID
        assert data["detected_main"] == "main.tex"

    def test_multiple_files(self, client):
        """A multi-file upload keeps every file, not only the last one.

        Source plus a companion asset (here a .bib) is the normal shape of a
        real project, and this list is what the file tree renders.
        """
        res = client.post("/api/upload", files=[
            ("files", ("main.tex", b"\\documentclass{article}\\begin{document}Hi\\end{document}", "text/plain")),
            ("files", ("refs.bib", b"@article{x, title={T}}", "text/plain")),
        ])
        assert res.status_code == 200 and len(res.json()["files"]) == 2

    def test_zip_upload(self, client):
        """A ZIP is expanded into the workspace, so its members show up as files.

        Uploading a .zip is how a whole project arrives; storing the archive
        verbatim would leave nothing for the compiler to build.
        """
        res = client.post("/api/upload", files=[
            ("files", ("project.zip", _make_zip({"main.tex": SIMPLE_TEX}), "application/zip"))])
        assert res.status_code == 200
        assert "main.tex" in [f["name"] for f in res.json()["files"]]

    def test_no_files(self, client):
        """Uploading nothing is refused instead of creating an empty session.

        Either status is correct: FastAPI's own validation answers 422 when the
        multipart part is absent entirely, the handler answers 400 when the list
        arrives but is empty.
        """
        assert client.post("/api/upload").status_code in (400, 422)

    def test_disallowed_extension(self, client):
        """A non-whitelisted extension (.py) is refused at the door.

        The extension whitelist is the first line of defence: files LaTeX never
        needs – scripts, executables – must not reach the directory the compiler
        later runs a subprocess in.
        """
        res = client.post("/api/upload",
                          files=[("files", ("evil.py", b"import os", "text/plain"))])
        assert res.status_code == 400

    def test_files_have_sizes(self, client):
        """Every listed file carries a ``size``, which the file tree displays."""
        session_id = _upload_simple(client)
        files = client.get(f"/api/files/{session_id}").json()["files"]
        assert all("size" in f for f in files)


# ─── GET / PUT /api/files ────────────────────────────────────────────────────

class TestFiles:
    """Listing a workspace and the editor's read/write round-trip."""

    def test_list_files(self, client):
        """Listing a live session answers a ``files`` array the tree can render."""
        session_id = _upload_simple(client)
        assert "files" in client.get(f"/api/files/{session_id}").json()

    def test_invalid_session(self, client):
        """A session id that is not a UUID is a 400, never a 404.

        The UI distinguishes them: 400 means "bad request, do not retry", 404
        means "the session expired, offer a fresh upload".
        """
        assert client.get("/api/files/not-a-uuid").status_code == 400

    def test_unknown_session(self, client):
        """A well-formed but unknown session id is a 404.

        The mirror image of the test above: parsing as a UUID buys no trust, the
        workspace has to actually exist.
        """
        assert client.get(f"/api/files/{uuid.uuid4()}").status_code == 404

    def test_read_and_write(self, client):
        """A PUT is visible to the next GET – the editor's save/reload loop.

        Proves the write landed in *this* session's workspace: had it gone
        anywhere else, the read would still return the original text and the
        user's edits would appear to vanish on reload.
        """
        session_id = _upload_simple(client)
        updated_tex = "\\documentclass{article}\\begin{document}Updated!\\end{document}"
        assert client.put(f"/api/files/{session_id}/main.tex",
                          content=updated_tex.encode()).status_code == 200
        assert client.get(f"/api/files/{session_id}/main.tex").text == updated_tex


# ─── POST /api/compile ───────────────────────────────────────────────────────

class TestCompile:
    """Argument validation for compile. The real build lives in TestEndToEnd,
    since only that needs a LaTeX distribution."""

    def test_invalid_session(self, client):
        """A malformed session id is rejected before any compile is attempted."""
        res = client.post("/api/compile", data={"session_id": "bad", "engine": "pdflatex"})
        assert res.status_code == 400

    def test_invalid_engine(self, client):
        """An engine outside the whitelist is refused.

        ``engine`` decides which binary gets executed, so it is checked against
        the ENGINES tuple instead of being handed to the subprocess layer.
        """
        session_id = _upload_simple(client)
        res = client.post("/api/compile", data={"session_id": session_id, "engine": "evil"})
        assert res.status_code == 400

    def test_traversal_main_file(self, client):
        """``main_file`` cannot point outside the session workspace.

        The route requires main_file to be one of the session's own .tex files.
        Without that check, a crafted path would make the compiler open – and
        report the contents of – files from the install directory.
        """
        session_id = _upload_simple(client)
        res = client.post("/api/compile", data={
            "session_id": session_id, "engine": "pdflatex",
            "main_file": "../../backend/main.py"})
        assert res.status_code == 400

    def test_response_shape(self, client):
        """Response is well-formed even when LaTeX is absent (compile may fail).

        The log panel is rendered from exactly these keys, so a machine without
        LaTeX must still get the full envelope – a failure is reported *inside*
        the structure, never as a bare 500 the UI cannot display.
        """
        session_id = _upload_simple(client)
        data = client.post("/api/compile",
                           data={"session_id": session_id, "engine": "pdflatex"}).json()
        for key in ("success", "summary", "log", "session_id", "engine"):
            assert key in data
        for key in ("errors", "warnings", "badboxes", "raw", "has_errors"):
            assert key in data["log"]


# ─── GET /api/pdf and /api/log ───────────────────────────────────────────────

class TestPdfAndLog:
    """Artifact retrieval before anything has been compiled."""

    def test_pdf_before_compile_404(self, client):
        """Asking for the PDF before compiling is a clean 404.

        Guards the "stale artifact" class of bug: the endpoint must not fall
        back to some other PDF sitting in the workspace, or a project that never
        compiled would appear to have produced a document.
        """
        session_id = _upload_simple(client)
        assert client.get(f"/api/pdf/{session_id}").status_code == 404

    def test_pdf_invalid_session(self, client):
        """A malformed id is a 400 here too – same contract as the files API."""
        assert client.get("/api/pdf/not-a-uuid").status_code == 400

    def test_log_before_compile_404(self, client):
        """No compile means no log: 404 rather than an empty 200.

        An empty 200 would render as "compiled with no errors", which is the
        opposite of the truth.
        """
        session_id = _upload_simple(client)
        assert client.get(f"/api/log/{session_id}").status_code == 404


# ─── DELETE /api/cleanup ─────────────────────────────────────────────────────

class TestCleanup:
    """The user's "discard my project" action."""

    def test_removes_session(self, client):
        """Cleanup deletes the workspace, so the session is gone afterwards.

        Deleting the files is the privacy promise of a local tool; the follow-up
        404 is what proves the directory really went away rather than just being
        dropped from the in-memory bookkeeping.
        """
        session_id = _upload_simple(client)
        assert client.delete(f"/api/cleanup/{session_id}").status_code == 200
        assert client.get(f"/api/files/{session_id}").status_code == 404

    def test_unknown_is_graceful(self, client):
        """Deleting an unknown-but-valid session succeeds instead of erroring.

        Cleanup is fired on page unload and races the background session
        reaper, so it is deliberately idempotent – "already gone" is the
        outcome the caller wanted.
        """
        assert client.delete(f"/api/cleanup/{uuid.uuid4()}").status_code == 200

    def test_invalid_format(self, client):
        """A malformed id is still rejected – idempotence is not a free pass."""
        assert client.delete("/api/cleanup/NOT-A-UUID").status_code == 400


# ─── GET / (the frontend shell) ──────────────────────────────────────────────

class TestFrontend:
    """The page itself, including the per-instance token injection."""

    def test_root_serves_html_with_token_injected(self, client):
        """The page is served as HTML with the token placeholder substituted.

        The token is minted per server start and pasted into the page, so only
        same-origin scripts can read it. If ``__STUDIO_TOKEN__`` survived into
        the response the browser would send that literal string as
        ``X-Studio-Token`` and the middleware would 403 every API call.
        """
        res = client.get("/")
        assert res.status_code == 200 and "text/html" in res.headers.get("content-type", "")
        # The placeholder must have been replaced by a real token.
        assert "__STUDIO_TOKEN__" not in res.text
        assert b"LaTeX" in res.content


# ─── End-to-end (needs a real LaTeX distribution) ────────────────────────────

class TestEndToEnd:
    """Upload → compile → fetch the PDF, against a real LaTeX install."""

    @requires_latex
    def test_full_flow(self, client):
        """The documented happy path really produces a PDF the browser can open.

        The only test that proves the endpoints compose. It follows the
        ``pdf_url`` the compile response advertises instead of rebuilding the
        URL, so a mismatch between what the server promises and what it serves
        is caught here; the %PDF magic bytes prove a document came back rather
        than an error page.
        """
        session_id = _upload_simple(client)
        data = client.post("/api/compile",
                           data={"session_id": session_id, "engine": "pdflatex"}).json()
        assert data["success"], data.get("summary")
        pdf_res = client.get(data["pdf_url"])
        assert pdf_res.status_code == 200 and pdf_res.content[:4] == b"%PDF"
        # download variant sets an attachment disposition
        download_res = client.get(f"/api/pdf/{session_id}?download=1")
        assert "attachment" in download_res.headers.get("content-disposition", "")
