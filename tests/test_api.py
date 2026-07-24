"""
test_api.py – FastAPI endpoint integration tests.

Uses the ``client`` fixture (a TestClient entered as a context manager, so the
app lifespan runs) and the autouse ``isolated_uploads`` fixture, both from
conftest.py. No real server or LaTeX install is required except where marked.
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


def _upload_simple(client) -> str:
    res = client.post("/api/upload",
                      files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))])
    assert res.status_code == 200
    return res.json()["session_id"]


def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestStatus:
    def test_status_fields(self, client):
        data = client.get("/api/status").json()
        for key in ("latex_available", "tools", "default_engine",
                    "shell_escape_enabled", "version"):
            assert key in data
        # Safe default must be reported.
        assert data["shell_escape_enabled"] is False


class TestUpload:
    def test_single_tex(self, client):
        res = client.post("/api/upload",
                          files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))])
        assert res.status_code == 200
        data = res.json()
        assert uuid.UUID(data["session_id"])          # a real UUID
        assert data["detected_main"] == "main.tex"

    def test_multiple_files(self, client):
        res = client.post("/api/upload", files=[
            ("files", ("main.tex", b"\\documentclass{article}\\begin{document}Hi\\end{document}", "text/plain")),
            ("files", ("refs.bib", b"@article{x, title={T}}", "text/plain")),
        ])
        assert res.status_code == 200 and len(res.json()["files"]) == 2

    def test_zip_upload(self, client):
        res = client.post("/api/upload", files=[
            ("files", ("project.zip", _make_zip({"main.tex": SIMPLE_TEX}), "application/zip"))])
        assert res.status_code == 200
        assert "main.tex" in [f["name"] for f in res.json()["files"]]

    def test_no_files(self, client):
        assert client.post("/api/upload").status_code in (400, 422)

    def test_disallowed_extension(self, client):
        res = client.post("/api/upload",
                          files=[("files", ("evil.py", b"import os", "text/plain"))])
        assert res.status_code == 400

    def test_files_have_sizes(self, client):
        sid = _upload_simple(client)
        files = client.get(f"/api/files/{sid}").json()["files"]
        assert all("size" in f for f in files)


class TestFiles:
    def test_list_files(self, client):
        sid = _upload_simple(client)
        assert "files" in client.get(f"/api/files/{sid}").json()

    def test_invalid_session(self, client):
        assert client.get("/api/files/not-a-uuid").status_code == 400

    def test_unknown_session(self, client):
        assert client.get(f"/api/files/{uuid.uuid4()}").status_code == 404

    def test_read_and_write(self, client):
        sid = _upload_simple(client)
        new = "\\documentclass{article}\\begin{document}Updated!\\end{document}"
        assert client.put(f"/api/files/{sid}/main.tex", content=new.encode()).status_code == 200
        assert client.get(f"/api/files/{sid}/main.tex").text == new


class TestCompile:
    def test_invalid_session(self, client):
        res = client.post("/api/compile", data={"session_id": "bad", "engine": "pdflatex"})
        assert res.status_code == 400

    def test_invalid_engine(self, client):
        sid = _upload_simple(client)
        res = client.post("/api/compile", data={"session_id": sid, "engine": "evil"})
        assert res.status_code == 400

    def test_traversal_main_file(self, client):
        sid = _upload_simple(client)
        res = client.post("/api/compile", data={
            "session_id": sid, "engine": "pdflatex", "main_file": "../../backend/main.py"})
        assert res.status_code == 400

    def test_response_shape(self, client):
        """Response is well-formed even when LaTeX is absent (compile may fail)."""
        sid = _upload_simple(client)
        data = client.post("/api/compile", data={"session_id": sid, "engine": "pdflatex"}).json()
        for key in ("success", "summary", "log", "session_id", "engine"):
            assert key in data
        for key in ("errors", "warnings", "badboxes", "raw", "has_errors"):
            assert key in data["log"]


class TestPdfAndLog:
    def test_pdf_before_compile_404(self, client):
        sid = _upload_simple(client)
        assert client.get(f"/api/pdf/{sid}").status_code == 404

    def test_pdf_invalid_session(self, client):
        assert client.get("/api/pdf/not-a-uuid").status_code == 400

    def test_log_before_compile_404(self, client):
        sid = _upload_simple(client)
        assert client.get(f"/api/log/{sid}").status_code == 404


class TestCleanup:
    def test_removes_session(self, client):
        sid = _upload_simple(client)
        assert client.delete(f"/api/cleanup/{sid}").status_code == 200
        assert client.get(f"/api/files/{sid}").status_code == 404

    def test_unknown_is_graceful(self, client):
        assert client.delete(f"/api/cleanup/{uuid.uuid4()}").status_code == 200

    def test_invalid_format(self, client):
        assert client.delete("/api/cleanup/NOT-A-UUID").status_code == 400


class TestFrontend:
    def test_root_serves_html_with_token_injected(self, client):
        res = client.get("/")
        assert res.status_code == 200 and "text/html" in res.headers.get("content-type", "")
        # The placeholder must have been replaced by a real token.
        assert "__STUDIO_TOKEN__" not in res.text
        assert b"LaTeX" in res.content


class TestEndToEnd:
    @requires_latex
    def test_full_flow(self, client):
        sid = _upload_simple(client)
        data = client.post("/api/compile", data={"session_id": sid, "engine": "pdflatex"}).json()
        assert data["success"], data.get("summary")
        pdf = client.get(data["pdf_url"])
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        # download variant sets an attachment disposition
        dl = client.get(f"/api/pdf/{sid}?download=1")
        assert "attachment" in dl.headers.get("content-disposition", "")
