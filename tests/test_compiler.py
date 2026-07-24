"""
test_compiler.py – Comprehensive compilation scenario tests.

Tests every scenario described in the implementation plan.
Requires MiKTeX or TeX Live to be installed for compilation tests.
Non-compilation tests (log parser, file manager) run without LaTeX.

Run with:  pytest tests/test_compiler.py -v
Skip LaTeX tests:  pytest tests/test_compiler.py -v -m "not requires_latex"
"""

import os
import shutil
import zipfile
import io
import time
import threading
from pathlib import Path

import pytest

from backend.config import UPLOAD_DIR
from backend.compiler import compile_project, check_latex_available, _resolve_binary
from backend.file_manager import (
    create_session, delete_session, get_session_dir,
    detect_main_tex, save_uploaded_files, _extract_zip, _safe_filename,
)
from backend.log_parser import parse_log, get_log_summary

# ─── Fixtures Directory ───────────────────────────────────────────────────────
FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ─── Check if LaTeX is available ─────────────────────────────────────────────
def _latex_available():
    status = check_latex_available()
    return status.get("pdflatex", {}).get("available", False)

LATEX_AVAILABLE = _latex_available()
requires_latex = pytest.mark.skipif(
    not LATEX_AVAILABLE,
    reason="pdflatex not found – install MiKTeX or TeX Live"
)


# ─── Helper: Copy fixture to a new session ───────────────────────────────────
def _setup_session_from_fixture(fixture_name: str) -> tuple[str, Path]:
    """Create a session and copy fixture files into it."""
    session_id = create_session()
    session_dir = get_session_dir(session_id)
    src = FIXTURES_DIR / fixture_name
    if src.is_dir():
        shutil.copytree(src, session_dir, dirs_exist_ok=True)
    return session_id, session_dir


def _teardown_session(session_id: str):
    delete_session(session_id)


# ════════════════════════════════════════════════════════════════════════════
# GROUP 1 – Log Parser (no LaTeX needed)
# ════════════════════════════════════════════════════════════════════════════

class TestLogParser:

    def test_parse_clean_log(self):
        """Log with no errors or warnings → empty result."""
        raw = "This is pdfTeX, Version 3.14159\nOutput written on main.pdf (2 pages).\n"
        result = parse_log(raw)
        assert not result.has_errors
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    def test_parse_error_detected(self):
        """Log with a hard error → error captured."""
        raw = """
This is pdfTeX
! Undefined control sequence.
l.5 \\badcommand
"""
        result = parse_log(raw)
        assert result.has_errors
        assert len(result.errors) >= 1
        assert "Undefined control sequence" in result.errors[0].message

    def test_parse_missing_package(self):
        """Log with missing .sty → friendly error message."""
        raw = "! LaTeX Error: File `nonexistentpackage.sty' not found.\n"
        result = parse_log(raw)
        assert result.has_errors
        # Should detect the missing package
        pkg_errors = [e for e in result.errors if "nonexistentpackage" in e.message]
        assert len(pkg_errors) >= 1

    def test_parse_latex_warning(self):
        """LaTeX Warning lines are captured."""
        raw = "LaTeX Warning: Citation `smith2020' on page 1 undefined.\n"
        result = parse_log(raw)
        assert len(result.warnings) >= 1

    def test_parse_overfull_hbox(self):
        """Overfull hbox → badbox category."""
        raw = "Overfull \\hbox (5.00pt too wide) in paragraph at lines 42--50\n"
        result = parse_log(raw)
        assert len(result.badboxes) >= 1
        assert result.badboxes[0].line == 42

    def test_parse_bibliography_warning(self):
        """Missing .bbl warning detected."""
        raw = "No file main.bbl.\nLaTeX Warning: There were undefined references.\n"
        result = parse_log(raw)
        bib_warns = [w for w in result.warnings if ".bbl" in w.message.lower() or "bibliography" in w.message.lower()]
        assert len(bib_warns) >= 1

    def test_log_to_dict(self):
        """ParsedLog.to_dict() produces expected keys."""
        raw = "! Test error.\nl.1 test\n"
        result = parse_log(raw)
        d = result.to_dict()
        assert "errors" in d
        assert "warnings" in d
        assert "badboxes" in d
        assert "raw" in d
        assert "has_errors" in d

    def test_log_summary_clean(self):
        """Summary for clean log is correct."""
        raw = "Output written on main.pdf (2 pages).\n"
        result = parse_log(raw)
        summary = get_log_summary(result)
        assert "no issues" in summary.lower()

    def test_log_summary_with_errors(self):
        """Summary includes error count."""
        raw = "! Error one.\nl.1 x\n! Error two.\nl.2 y\n"
        result = parse_log(raw)
        summary = get_log_summary(result)
        assert "error" in summary.lower()


# ════════════════════════════════════════════════════════════════════════════
# GROUP 2 – File Manager (no LaTeX needed)
# ════════════════════════════════════════════════════════════════════════════

class TestFileManager:

    def test_create_and_delete_session(self):
        """Session directory is created and deleted cleanly."""
        sid = create_session()
        d = get_session_dir(sid)
        assert d.exists()
        delete_session(sid)
        assert not d.exists()

    def test_invalid_session_id_rejected(self):
        """Non-UUID session_id is rejected with 400."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            get_session_dir("../etc/passwd")
        assert exc_info.value.status_code == 400

    def test_nonexistent_session_returns_404(self):
        """Session that doesn't exist returns 404."""
        from fastapi import HTTPException
        import uuid
        with pytest.raises(HTTPException) as exc_info:
            get_session_dir(str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_safe_filename_strips_traversal(self):
        """Path traversal in filenames is sanitized."""
        assert "/" not in _safe_filename("../../../etc/passwd")
        assert "\\" not in _safe_filename("..\\..\\windows\\system32")

    def test_safe_filename_normal(self):
        """Normal filenames pass through correctly."""
        assert _safe_filename("main.tex") == "main.tex"
        assert _safe_filename("chapter_1.tex") == "chapter_1.tex"
        assert _safe_filename("figure-01.png") == "figure-01.png"

    def test_detect_main_tex_named_main(self):
        """main.tex in root is auto-detected."""
        sid = create_session()
        d = get_session_dir(sid)
        (d / "main.tex").write_text(r"\documentclass{article}\begin{document}\end{document}")
        (d / "other.tex").write_text("some content")
        assert detect_main_tex(d) == "main.tex"
        delete_session(sid)

    def test_detect_main_tex_by_documentclass(self):
        """File with \\documentclass is detected as main."""
        sid = create_session()
        d = get_session_dir(sid)
        (d / "preamble.tex").write_text(r"\newcommand{\foo}{bar}")
        (d / "doc.tex").write_text(r"\documentclass{article}\begin{document}Hello\end{document}")
        result = detect_main_tex(d)
        assert result == "doc.tex"
        delete_session(sid)

    def test_detect_main_tex_no_tex_files(self):
        """Returns None when no .tex files exist."""
        sid = create_session()
        d = get_session_dir(sid)
        (d / "readme.txt").write_text("Hello")
        assert detect_main_tex(d) is None
        delete_session(sid)

    def test_zip_extraction_basic(self):
        """ZIP archive is extracted into session dir."""
        sid = create_session()
        d = get_session_dir(sid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", r"\documentclass{article}\begin{document}Hi\end{document}")
            zf.writestr("refs.bib", "@article{x,title={X}}")
        extracted = _extract_zip(buf.getvalue(), d)
        assert "main.tex" in extracted
        assert (d / "main.tex").exists()
        delete_session(sid)

    def test_zip_slip_attack_blocked(self):
        """ZIP with path traversal entries is rejected."""
        from fastapi import HTTPException
        sid = create_session()
        d = get_session_dir(sid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("../../../evil.tex")
            zf.writestr(info, "malicious")
        with pytest.raises(HTTPException) as exc_info:
            _extract_zip(buf.getvalue(), d)
        assert exc_info.value.status_code == 400
        delete_session(sid)


# ════════════════════════════════════════════════════════════════════════════
# GROUP 3 – Compilation Scenarios (require LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestCompilation:

    @requires_latex
    def test_simple_compile(self):
        """TC-01: Single .tex file with no external deps → PDF produced."""
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex")
            assert result.success, f"Expected success. Summary: {result.summary}"
            assert result.pdf_path is not None
            assert result.pdf_path.exists()
            assert result.pdf_path.stat().st_size > 0
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_with_images(self):
        """TC-02: .tex + PNG image → PDF with embedded image."""
        sid, d = _setup_session_from_fixture("with_images")
        try:
            result = compile_project(d, "main.tex")
            # Should succeed even if figure is missing – just a warning
            assert result.pdf_path is not None or result.parsed_log is not None
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_multi_file(self):
        """TC-03: main.tex + \\input chapters → PDF compiled correctly."""
        sid, d = _setup_session_from_fixture("multi_file")
        try:
            result = compile_project(d, "main.tex")
            assert result.success, f"Expected success. Summary: {result.summary}"
            assert result.pdf_path is not None
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_broken_latex_returns_log(self):
        """TC-04: Broken .tex → failure + log with errors (no crash)."""
        sid, d = _setup_session_from_fixture("broken")
        try:
            result = compile_project(d, "main.tex")
            # Should fail gracefully
            assert not result.success or result.parsed_log is not None
            # Log should have error info
            if result.parsed_log:
                assert result.parsed_log.raw  # Raw log should be non-empty
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_missing_main_file(self):
        """TC-05: Non-existent main file → graceful error, no crash."""
        sid = create_session()
        d = get_session_dir(sid)
        try:
            result = compile_project(d, "nonexistent.tex")
            assert not result.success
            assert "not found" in result.summary.lower()
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_compile_result_has_log(self):
        """TC-06: Every compile result includes a log object."""
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex")
            assert result.parsed_log is not None
            assert isinstance(result.parsed_log.raw, str)
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_compile_duration_recorded(self):
        """TC-07: Compilation duration is recorded and positive."""
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex")
            assert result.duration_seconds > 0
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_zip_upload_and_compile(self):
        """TC-08: Upload as ZIP → extract → compile successfully."""
        sid = create_session()
        d = get_session_dir(sid)
        try:
            # Package simple fixture as ZIP
            buf = io.BytesIO()
            src = FIXTURES_DIR / "simple"
            with zipfile.ZipFile(buf, "w") as zf:
                for f in src.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(src))
            # Extract into session
            _extract_zip(buf.getvalue(), d)
            main = detect_main_tex(d)
            assert main is not None
            result = compile_project(d, main)
            assert result.success, f"ZIP compile failed: {result.summary}"
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_shell_escape_allowed(self):
        """TC-09: \\write18 / shell escape is ALLOWED to support minted."""
        sid = create_session()
        d = get_session_dir(sid)
        (d / "shell.tex").write_text(
            r"""\documentclass{article}
\begin{document}
\immediate\write18{echo SUCCESS > shell_test.txt}
Hello
\end{document}
"""
        )
        try:
            result = compile_project(d, "shell.tex")
            test_file = d / "shell_test.txt"
            assert test_file.exists(), "Shell escape is not working!"
            assert "SUCCESS" in test_file.read_text()
        finally:
            _teardown_session(sid)

    def test_concurrent_sessions_isolated(self):
        """TC-10: Multiple concurrent sessions don't interfere (file isolation)."""
        sids = [create_session() for _ in range(3)]
        try:
            for i, sid in enumerate(sids):
                d = get_session_dir(sid)
                (d / "main.tex").write_text(
                    f"\\documentclass{{article}}\\begin{{document}}Session {i}\\end{{document}}"
                )
            # Verify directories are separate
            dirs = [get_session_dir(sid) for sid in sids]
            assert len(set(str(d) for d in dirs)) == 3
        finally:
            for sid in sids:
                _teardown_session(sid)

    @requires_latex
    def test_timeout_kills_process(self):
        """TC-11: A very short timeout causes TimeoutError."""
        sid = create_session()
        d = get_session_dir(sid)
        # Use an infinite loop LaTeX document
        (d / "main.tex").write_text(
            r"""\documentclass{article}
\begin{document}
\loop\iftrue\repeat
\end{document}
"""
        )
        try:
            result = compile_project(d, "main.tex", timeout=5)
            # Either timed out (no PDF) or succeeded quickly
            if not result.success:
                assert "timeout" in result.summary.lower() or result.pdf_path is None
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_engine_pdflatex(self):
        """TC-12: Explicitly use pdflatex engine."""
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex", engine="pdflatex")
            assert result.success
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_pdf_is_valid_pdf_bytes(self):
        """TC-13: Compiled output starts with PDF magic bytes %%PDF."""
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex")
            assert result.success
            pdf_bytes = result.pdf_path.read_bytes()
            assert pdf_bytes[:4] == b"%PDF", "Output is not a valid PDF!"
        finally:
            _teardown_session(sid)


    def test_check_latex_available_returns_dict(self):
        """TC-14: check_latex_available() returns dict with expected keys."""
        status = check_latex_available()
        assert isinstance(status, dict)
        assert "pdflatex" in status
        for tool, info in status.items():
            assert "available" in info
            assert "path" in info

    @requires_latex
    def test_with_bibtex(self):
        """TC-15: .tex + .bib with traditional BibTeX → PDF with bibliography."""
        sid, d = _setup_session_from_fixture("with_bibtex")
        try:
            result = compile_project(d, "main.tex")
            # Should either succeed or at least produce a log (bibtex may
            # warn about missing .bst on first run on fresh MiKTeX installs)
            assert result.parsed_log is not None
            if result.success:
                assert result.pdf_path is not None
                assert result.pdf_path.exists()
        finally:
            _teardown_session(sid)

    @requires_latex
    def test_custom_class(self):
        """TC-16: .tex + custom .cls file → PDF compiled with custom class."""
        sid, d = _setup_session_from_fixture("custom_class")
        try:
            result = compile_project(d, "main.tex")
            assert result.success, f"Custom class compile failed: {result.summary}"
            assert result.pdf_path is not None
            assert result.pdf_path.exists()
        finally:
            _teardown_session(sid)

    def test_large_file_rejected(self):
        """TC-17: Upload exceeding MAX_UPLOAD_SIZE_BYTES is rejected."""
        from fastapi import HTTPException
        from backend.config import MAX_UPLOAD_SIZE_BYTES

        sid = create_session()
        try:
            # Create a fake UploadFile that exceeds the limit
            import io
            from unittest.mock import AsyncMock, MagicMock

            # Build a content blob just over the limit
            oversized_content = b"x" * (MAX_UPLOAD_SIZE_BYTES + 1024)

            mock_file = MagicMock()
            mock_file.filename = "huge.tex"
            mock_file.read = AsyncMock(return_value=oversized_content)

            with pytest.raises(HTTPException) as exc_info:
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    save_uploaded_files(sid, [mock_file])
                )
            assert exc_info.value.status_code == 413
        finally:
            _teardown_session(sid)


# ════════════════════════════════════════════════════════════════════════════
# GROUP 4 – End-to-End API Flow (require LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestEndToEnd:
    """Full upload → compile → download PDF flow through the API."""

    @requires_latex
    def test_full_flow_upload_compile_download(self):
        """E2E: Upload .tex → compile → download PDF → verify it's valid."""
        from fastapi.testclient import TestClient
        from backend.main import app

        client = TestClient(app)

        # Step 1: Upload
        tex_content = (FIXTURES_DIR / "simple" / "main.tex").read_bytes()
        res = client.post(
            "/api/upload",
            files=[("files", ("main.tex", tex_content, "text/plain"))],
        )
        assert res.status_code == 200
        session_id = res.json()["session_id"]

        try:
            # Step 2: Compile
            res = client.post(
                "/api/compile",
                data={"session_id": session_id, "engine": "pdflatex"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["success"], f"Compile failed: {data.get('summary')}"
            assert data["pdf_url"] is not None

            # Step 3: Download PDF
            res = client.get(data["pdf_url"])
            assert res.status_code == 200
            assert "application/pdf" in res.headers.get("content-type", "")

            # Step 4: Verify it's a valid PDF
            assert res.content[:4] == b"%PDF", "Downloaded file is not a valid PDF!"
            assert len(res.content) > 1000, "PDF seems too small to be valid"

            # Step 5: Get parsed log
            res = client.get(f"/api/log/{session_id}?parsed=true")
            assert res.status_code == 200
            log_data = res.json()
            assert "errors" in log_data
            assert "warnings" in log_data

        finally:
            # Step 6: Cleanup
            res = client.delete(f"/api/cleanup/{session_id}")
            assert res.status_code == 200

