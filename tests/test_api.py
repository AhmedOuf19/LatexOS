"""
test_api.py – FastAPI endpoint integration tests.

Tests all REST API endpoints using FastAPI's TestClient (synchronous HTTP).
Does NOT require a running server – uses ASGI transport directly.

Run with:  pytest tests/test_api.py -v
"""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app

# ─── Test Client ─────────────────────────────────────────────────────────────
client = TestClient(app)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SIMPLE_TEX = (FIXTURES_DIR / "simple" / "main.tex").read_text()


# ─── Helper ───────────────────────────────────────────────────────────────────
def _upload_simple() -> str:
    """Upload a simple .tex file and return the session_id."""
    res = client.post(
        "/api/upload",
        files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))],
    )
    assert res.status_code == 200
    return res.json()["session_id"]


def _make_zip_bytes(files: dict) -> bytes:
    """Create an in-memory ZIP from a dict of {filename: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ════════════════════════════════════════════════════════════════════════════
# GROUP 1 – Status Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestStatus:

    def test_status_endpoint_reachable(self):
        """GET /api/status returns 200."""
        res = client.get("/api/status")
        assert res.status_code == 200

    def test_status_has_required_fields(self):
        """Status response has required fields."""
        res = client.get("/api/status")
        data = res.json()
        assert "latex_available" in data
        assert "tools" in data
        assert "default_engine" in data

    def test_status_tools_have_available_field(self):
        """Each tool in status has 'available' and 'path' keys."""
        res = client.get("/api/status")
        tools = res.json()["tools"]
        assert "pdflatex" in tools
        for tool_info in tools.values():
            assert "available" in tool_info
            assert "path" in tool_info


# ════════════════════════════════════════════════════════════════════════════
# GROUP 2 – Upload Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestUpload:

    def test_upload_single_tex(self):
        """POST /api/upload with a .tex file → 200 with session_id."""
        res = client.post(
            "/api/upload",
            files=[("files", ("main.tex", b"\\documentclass{article}\\begin{document}Hi\\end{document}", "text/plain"))],
        )
        assert res.status_code == 200
        data = res.json()
        assert "session_id" in data
        assert len(data["session_id"]) == 36  # UUID
        assert "files" in data
        assert len(data["files"]) >= 1
        # Cleanup
        client.delete(f"/api/cleanup/{data['session_id']}")

    def test_upload_multiple_files(self):
        """Upload multiple files at once."""
        tex = b"\\documentclass{article}\\begin{document}Hi\\end{document}"
        bib = b"@article{x, title={Test}}"
        res = client.post(
            "/api/upload",
            files=[
                ("files", ("main.tex", tex, "text/plain")),
                ("files", ("refs.bib", bib, "text/plain")),
            ],
        )
        assert res.status_code == 200
        data = res.json()
        assert len(data["files"]) == 2
        client.delete(f"/api/cleanup/{data['session_id']}")

    def test_upload_zip_file(self):
        """Upload a ZIP archive → extracted files returned."""
        zip_bytes = _make_zip_bytes({
            "main.tex": SIMPLE_TEX,
            "refs.bib": "@article{x, title={Test}}",
        })
        res = client.post(
            "/api/upload",
            files=[("files", ("project.zip", zip_bytes, "application/zip"))],
        )
        assert res.status_code == 200
        data = res.json()
        names = [f["name"] for f in data["files"]]
        assert "main.tex" in names
        client.delete(f"/api/cleanup/{data['session_id']}")

    def test_upload_no_files_returns_400(self):
        """Upload with no files → 422 or 400."""
        res = client.post("/api/upload")
        assert res.status_code in (400, 422)

    def test_upload_disallowed_extension_rejected(self):
        """Upload a .py file → 400 Bad Request."""
        res = client.post(
            "/api/upload",
            files=[("files", ("evil.py", b"import os; os.system('rm -rf /')", "text/plain"))],
        )
        assert res.status_code == 400

    def test_upload_auto_detects_main_tex(self):
        """Upload returns detected_main for a single .tex file."""
        res = client.post(
            "/api/upload",
            files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))],
        )
        assert res.status_code == 200
        data = res.json()
        assert data["detected_main"] == "main.tex"
        client.delete(f"/api/cleanup/{data['session_id']}")

    def test_upload_returns_file_sizes(self):
        """Uploaded file entries include size field."""
        res = client.post(
            "/api/upload",
            files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))],
        )
        assert res.status_code == 200
        files = res.json()["files"]
        assert all("size" in f for f in files)
        client.delete(f"/api/cleanup/{res.json()['session_id']}")


# ════════════════════════════════════════════════════════════════════════════
# GROUP 3 – Files Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestFiles:

    def test_list_files_for_session(self):
        """GET /api/files/{session} returns file list."""
        sid = _upload_simple()
        try:
            res = client.get(f"/api/files/{sid}")
            assert res.status_code == 200
            data = res.json()
            assert "files" in data
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_list_files_invalid_session(self):
        """GET /api/files/{bad_id} returns 400."""
        res = client.get("/api/files/not-a-uuid-at-all")
        assert res.status_code == 400

    def test_list_files_nonexistent_session(self):
        """GET /api/files/{unknown_uuid} returns 404."""
        import uuid
        res = client.get(f"/api/files/{uuid.uuid4()}")
        assert res.status_code == 404


# ════════════════════════════════════════════════════════════════════════════
# GROUP 4 – Compile Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestCompile:

    def test_compile_invalid_session(self):
        """POST /api/compile with bad session_id → 400."""
        res = client.post(
            "/api/compile",
            data={"session_id": "not-a-real-uuid-format-x", "engine": "pdflatex"},
        )
        assert res.status_code == 400

    def test_compile_invalid_engine_rejected(self):
        """POST /api/compile with invalid engine → 400."""
        sid = _upload_simple()
        try:
            res = client.post(
                "/api/compile",
                data={"session_id": sid, "engine": "maliciousengine"},
            )
            assert res.status_code == 400
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_compile_path_traversal_main_file(self):
        """POST /api/compile with path traversal in main_file → 400."""
        sid = _upload_simple()
        try:
            res = client.post(
                "/api/compile",
                data={
                    "session_id": sid,
                    "engine": "pdflatex",
                    "main_file": "../../backend/main.py",
                },
            )
            assert res.status_code in (400, 422)
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_compile_response_has_required_fields(self):
        """Compile response always contains required fields."""
        sid = _upload_simple()
        try:
            res = client.post(
                "/api/compile",
                data={"session_id": sid, "engine": "pdflatex"},
            )
            assert res.status_code == 200
            data = res.json()
            assert "success" in data
            assert "summary" in data
            assert "log" in data
            assert "session_id" in data
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_compile_response_log_has_structure(self):
        """Log in compile response has errors/warnings/raw fields."""
        sid = _upload_simple()
        try:
            res = client.post(
                "/api/compile",
                data={"session_id": sid, "engine": "pdflatex"},
            )
            assert res.status_code == 200
            log = res.json().get("log", {})
            # These keys must ALWAYS exist (even when LaTeX isn't installed)
            for key in ("errors", "warnings", "badboxes", "raw"):
                assert key in log, (
                    f"Missing key '{key}' in log. "
                    f"Log keys present: {list(log.keys())}. "
                    "Ensure compiler.py always returns a fully-structured log dict."
                )
        finally:
            client.delete(f"/api/cleanup/{sid}")


# ════════════════════════════════════════════════════════════════════════════
# GROUP 5 – PDF Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestPDF:

    def test_get_pdf_before_compile_returns_404(self):
        """GET /api/pdf/{session} before compile → 404."""
        sid = _upload_simple()
        try:
            res = client.get(f"/api/pdf/{sid}")
            assert res.status_code == 404
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_get_pdf_invalid_session(self):
        """GET /api/pdf/{bad_id} → 400."""
        res = client.get("/api/pdf/../../etc/passwd")
        assert res.status_code in (400, 404)

    def test_get_pdf_unknown_session(self):
        """GET /api/pdf/{valid_but_unknown_uuid} → 404."""
        import uuid
        res = client.get(f"/api/pdf/{uuid.uuid4()}")
        assert res.status_code == 404


# ════════════════════════════════════════════════════════════════════════════
# GROUP 6 – Log Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestLog:

    def test_get_log_before_compile_returns_404(self):
        """GET /api/log/{session} before compile → 404."""
        sid = _upload_simple()
        try:
            res = client.get(f"/api/log/{sid}")
            assert res.status_code == 404
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_get_log_invalid_session(self):
        """GET /api/log/{bad_id} → 400."""
        res = client.get("/api/log/not-a-uuid-xyz")
        assert res.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# GROUP 7 – Cleanup Endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestCleanup:

    def test_cleanup_removes_session(self):
        """DELETE /api/cleanup/{session} → session directory removed."""
        sid = _upload_simple()
        from backend.config import UPLOAD_DIR
        session_dir = UPLOAD_DIR / sid
        assert session_dir.exists()

        res = client.delete(f"/api/cleanup/{sid}")
        assert res.status_code == 200
        assert not session_dir.exists()

    def test_cleanup_nonexistent_session_is_graceful(self):
        """DELETE /api/cleanup/{unknown} → 200 (already gone, that's fine)."""
        import uuid
        res = client.delete(f"/api/cleanup/{uuid.uuid4()}")
        assert res.status_code == 200

    def test_cleanup_invalid_session_returns_400(self):
        """DELETE /api/cleanup/{bad_format} → 400."""
        # Use a clearly invalid UUID format (no slashes to avoid routing confusion)
        res = client.delete("/api/cleanup/NOT-A-VALID-UUID-FORMAT-AT-ALL")
        assert res.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# GROUP 8 – Frontend Serving
# ════════════════════════════════════════════════════════════════════════════

class TestFrontend:

    def test_root_serves_html(self):
        """GET / serves the index.html page."""
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")
        assert b"LaTeX" in res.content

# ════════════════════════════════════════════════════════════════════════════
# GROUP 9 – IDE Editor Endpoints
# ════════════════════════════════════════════════════════════════════════════

class TestEditorEndpoints:

    def test_read_file_success(self):
        """GET /api/files/{session}/{filepath} returns file content."""
        sid = _upload_simple()
        try:
            res = client.get(f"/api/files/{sid}/main.tex")
            assert res.status_code == 200
            assert "text/plain" in res.headers["content-type"]
            assert "\\begin{document}" in res.text
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_write_file_success(self):
        """PUT /api/files/{session}/{filepath} updates file content."""
        sid = _upload_simple()
        try:
            new_content = "\\documentclass{article}\\begin{document}Updated!\\end{document}"
            res = client.put(
                f"/api/files/{sid}/main.tex",
                content=new_content.encode(),
            )
            assert res.status_code == 200
            
            # Verify it was written
            res2 = client.get(f"/api/files/{sid}/main.tex")
            assert res2.text == new_content
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_read_file_path_traversal_blocked(self):
        """GET /api/files/{session}/../../etc/passwd is blocked."""
        sid = _upload_simple()
        try:
            res = client.get(f"/api/files/{sid}/..%2F..%2Fetc%2Fpasswd")
            assert res.status_code == 400
            assert "traversal" in res.text.lower()
        finally:
            client.delete(f"/api/cleanup/{sid}")

    def test_write_file_path_traversal_blocked(self):
        """PUT /api/files/{session}/../../evil.py is blocked."""
        sid = _upload_simple()
        try:
            res = client.put(
                f"/api/files/{sid}/..%2F..%2Fevil.py",
                content=b"evil",
            )
            assert res.status_code == 400
            assert "traversal" in res.text.lower()
        finally:
            client.delete(f"/api/cleanup/{sid}")
