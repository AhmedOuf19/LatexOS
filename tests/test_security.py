"""
test_security.py – regression tests, one per attack class the audit found.

Each test fails if its guard regresses. These are the safety net for the
Phase 0 security fixes; keep them green.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SIMPLE_TEX = (FIXTURES_DIR / "simple" / "main.tex").read_text()

requires_latex = pytest.mark.requires_latex


def _upload_simple(client) -> str:
    res = client.post("/api/upload",
                      files=[("files", ("main.tex", SIMPLE_TEX.encode(), "text/plain"))])
    return res.json()["session_id"]


class TestCrossOriginAndToken:
    def test_cross_origin_api_refused(self, client):
        """A request claiming a foreign Origin is refused (drive-by defence)."""
        res = client.get("/api/status", headers={"Origin": "http://evil.example"})
        assert res.status_code == 403

    def test_localhost_origin_allowed(self, client):
        res = client.get("/api/status", headers={"Origin": "http://127.0.0.1:8000"})
        assert res.status_code == 200

    def test_no_origin_allowed(self, client):
        """Non-browser clients (no Origin) are allowed – the loopback bind is
        their boundary."""
        assert client.get("/api/status").status_code == 200

    def test_wrong_token_refused(self, client):
        res = client.get("/api/status", headers={"X-Studio-Token": "definitely-wrong"})
        assert res.status_code == 403

    def test_correct_token_allowed(self, client):
        token = client.app.state.token
        res = client.get("/api/status", headers={"X-Studio-Token": token})
        assert res.status_code == 200


class TestPathTraversal:
    def test_static_route_traversal_blocked(self, client):
        """Regression (C5): the catch-all static route must not read outside
        the frontend directory."""
        for attempt in (
            "/%2e%2e/backend/config.py",
            "/../backend/config.py",
            "/%2e%2e%2f%2e%2e%2fbackend%2fconfig.py",
        ):
            res = client.get(attempt)
            assert res.status_code in (400, 404)
            assert "ALLOW_SHELL_ESCAPE" not in res.text  # config.py content must not leak

    def test_file_api_encoded_traversal_blocked(self, client):
        sid = _upload_simple(client)
        res = client.get(f"/api/files/{sid}/..%2f..%2fbackend%2fmain.py")
        assert res.status_code == 400

    def test_file_api_write_traversal_blocked(self, client):
        sid = _upload_simple(client)
        res = client.put(f"/api/files/{sid}/..%2f..%2fevil.py", content=b"x")
        assert res.status_code == 400

    def test_ads_colon_rejected(self, client):
        sid = _upload_simple(client)
        # An NTFS alternate-data-stream path contains ':' and must be rejected.
        res = client.get(f"/api/files/{sid}/main.tex:stream")
        assert res.status_code == 400


class TestUploadLimits:
    def test_upload_size_cap(self, client, monkeypatch):
        import backend.file_manager as fm
        monkeypatch.setattr(fm, "MAX_UPLOAD_SIZE_BYTES", 50)
        res = client.post("/api/upload",
                          files=[("files", ("big.tex", b"x" * 500, "text/plain"))])
        assert res.status_code == 413

    def test_put_size_cap(self, client, monkeypatch):
        import backend.file_manager as fm
        monkeypatch.setattr(fm, "MAX_UPLOAD_SIZE_BYTES", 50)
        sid = client.post("/api/upload",
                          files=[("files", ("main.tex", b"tiny", "text/plain"))]).json()["session_id"]
        res = client.put(f"/api/files/{sid}/main.tex", content=b"y" * 500)
        assert res.status_code == 413

    def test_zip_extensionless_dropped(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", SIMPLE_TEX)
            zf.writestr("latexmkrc", "system('calc');")
        res = client.post("/api/upload",
                          files=[("files", ("p.zip", buf.getvalue(), "application/zip"))])
        names = [f["name"] for f in res.json()["files"]]
        assert "main.tex" in names and "latexmkrc" not in names


class TestShellEscape:
    @requires_latex
    def test_write18_blocked_by_default(self):
        """Regression (C1): \\write18 must NOT run a command by default, and the
        document should still compile."""
        from backend.compiler import compile_project
        from backend.file_manager import create_session, delete_session, get_session_dir
        sid = create_session()
        d = get_session_dir(sid)
        (d / "main.tex").write_text(
            "\\documentclass{article}\\begin{document}"
            "\\immediate\\write18{echo pwned > pwned.txt}Hello\\end{document}")
        try:
            compile_project(d, "main.tex")
            assert not (d / "pwned.txt").exists(), "shell-escape must be disabled by default"
        finally:
            delete_session(sid)
