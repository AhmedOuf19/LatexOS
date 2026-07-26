"""
test_compiler.py – log parser, file manager, and compilation-scenario tests.

Non-LaTeX tests (parser, file manager, path safety, engine flag maps) always
run. Compilation tests are marked @requires_latex and auto-skip when no LaTeX
distribution is installed (see conftest.py). Every assertion is written so it
would actually FAIL if the feature it covers regressed.
"""

from __future__ import annotations

import io
import shutil
import threading
import zipfile
from pathlib import Path

import pytest

from backend.compiler import (
    _build_env,
    _engine_cmd,
    _latexmk_cmd,
    check_latex_available,
    compile_project,
)
from backend.file_manager import (
    _extract_zip,
    _safe_filename,
    create_session,
    delete_session,
    detect_main_tex,
    get_session_dir,
)
from backend.log_parser import get_log_summary, parse_log

FIXTURES_DIR = Path(__file__).parent / "fixtures"

requires_latex = pytest.mark.requires_latex


def _setup_session_from_fixture(fixture_name: str):
    sid = create_session()
    d = get_session_dir(sid)
    src = FIXTURES_DIR / fixture_name
    if src.is_dir():
        shutil.copytree(src, d, dirs_exist_ok=True)
    return sid, d


def _engine_available(engine: str) -> bool:
    return check_latex_available().get(engine, {}).get("available", False)


# ════════════════════════════════════════════════════════════════════════════
# GROUP 1 – Log parser (no LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestLogParser:

    def test_clean_log_has_no_errors(self):
        result = parse_log("This is pdfTeX\nOutput written on main.pdf (2 pages).\n")
        assert not result.has_errors
        assert result.errors == [] and result.warnings == []

    def test_bang_error_captured(self):
        result = parse_log("! Emergency stop.\nl.10 \\end{document}\n")
        assert result.has_errors
        assert "Emergency stop" in result.errors[0].message
        assert result.errors[0].line == 10

    def test_file_line_error_captured(self):
        """Regression: -file-line-error errors have no '! ' prefix and were
        previously dropped entirely."""
        raw = "(./main.tex\n./main.tex:4: Undefined control sequence.\nl.4 \\badcmd\n"
        result = parse_log(raw)
        assert result.has_errors, "file:line error must be captured"
        e = result.errors[0]
        assert e.line == 4 and "main.tex" in e.file
        assert "Undefined control sequence" in e.message

    def test_missing_package_hint(self):
        result = parse_log("! LaTeX Error: File `nonexistentpackage.sty' not found.\n")
        assert any("nonexistentpackage" in e.message for e in result.errors)

    def test_file_line_errors_without_whitelist_words(self):
        """Regression: errors whose text lacked whitelisted keywords (Too many
        }'s, Double superscript, Illegal unit, Misplaced alignment) used to be
        silently dropped, so a broken document reported as clean."""
        for msg in ("Too many }'s.", "Double superscript.",
                    "Illegal unit of measure (pt inserted).",
                    "Misplaced alignment tab character &."):
            result = parse_log(f"./doc.tex:5: {msg}\nl.5 ...\n")
            assert result.has_errors, f"must capture: {msg}"

    def test_windows_drive_letter_path_error(self):
        """Regression: errors reported from an absolute drive-lettered path
        (folder-local TinyTeX) must still parse."""
        raw = "c:/proj/tinytex/texmf-dist/tex/latex/foo/foo.sty:88: Undefined control sequence.\n"
        result = parse_log(raw)
        assert result.has_errors
        assert result.errors[0].file.startswith("c:/")

    def test_shell_escape_hint(self):
        """A minted failure must tell the user how to enable shell-escape."""
        raw = ("chapter3.tex:114: Package minted Error: Cannot highlight code "
               "(minted executable is unavailable or disabled).\n")
        result = parse_log(raw)
        assert any("Shell-escape" in e.message for e in result.errors)

    def test_bookmark_hint(self):
        """The \\@@BOOKMARK runaway must be explained in plain language."""
        raw = ("Runaway argument?\n{\\376\\377\\000S\\000p\\ETC.\n"
               "main.tex:208: File ended while scanning use of \\@@BOOKMARK.\n")
        result = parse_log(raw)
        assert any("bookmark" in e.message.lower() for e in result.errors)

    def test_unicode_hint_suggests_engine_switch(self):
        """Unicode-character errors should point at xelatex/lualatex."""
        raw = ("main.tex:12: LaTeX Error: Unicode character \u03b8 (U+03B8)\n"
               "               not set up for use with LaTeX.\n")
        result = parse_log(raw)
        hints = [e.message for e in result.errors if "xelatex" in e.message]
        assert hints and "U+03B8" in hints[0]

    def test_no_false_bbl_warning(self):
        """Regression: a successful build that loads (./main.bbl) plus a benign
        'No file main.out.' line must NOT report a missing bibliography."""
        result = parse_log("(./main.bbl)\nNo file main.out.\nOutput written on main.pdf (1 page).\n")
        assert not any(".bbl" in w.message for w in result.warnings)

    def test_seventy_nine_char_line_does_not_swallow_error(self):
        """Regression: a coincidental 79-char line must not merge with a
        following '! ...' error and hide it."""
        result = parse_log(("X" * 79) + "\n! Undefined control sequence.\nl.1 x\n")
        assert result.has_errors

    def test_latex_warning_captured(self):
        result = parse_log("LaTeX Warning: Citation `smith2020' on page 1 undefined.\n")
        assert len(result.warnings) >= 1
        assert "smith2020" in result.warnings[0].message

    def test_overfull_hbox_is_badbox(self):
        result = parse_log("Overfull \\hbox (5.0pt too wide) in paragraph at lines 42--50\n")
        assert len(result.badboxes) == 1
        assert result.badboxes[0].line == 42

    def test_duplicate_entries_deduped(self):
        """The same badbox on many pages should appear once."""
        line = "Overfull \\hbox (5.0pt too wide) in paragraph at lines 42--50\n"
        result = parse_log(line * 5)
        assert len(result.badboxes) == 1

    def test_to_dict_keys(self):
        d = parse_log("! Test error.\nl.1 x\n").to_dict()
        for key in ("errors", "warnings", "badboxes", "raw", "has_errors"):
            assert key in d

    def test_summary_clean_vs_errors(self):
        assert "no issues" in get_log_summary(parse_log("Output written.\n")).lower()
        assert "error" in get_log_summary(parse_log("! e1.\nl.1 x\n! e2.\nl.2 y\n")).lower()


# ════════════════════════════════════════════════════════════════════════════
# GROUP 2 – File manager & path safety (no LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestFileManager:

    def test_create_and_delete_session(self):
        sid = create_session()
        d = get_session_dir(sid)
        assert d.exists()
        delete_session(sid)
        assert not d.exists()

    def test_invalid_session_id_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            get_session_dir("../etc/passwd")
        assert exc.value.status_code == 400

    def test_noncanonical_uuid_rejected(self):
        """36 chars of [0-9a-f-] is not enough – must be a canonical UUID."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            get_session_dir("-" * 36)

    def test_safe_filename_strips_traversal(self):
        assert "/" not in _safe_filename("../../../etc/passwd")
        assert "\\" not in _safe_filename("..\\..\\windows\\system32")

    def test_safe_filename_neutralises_dotfiles(self):
        """A leading dot is stripped, so .latexmkrc cannot be created."""
        assert not _safe_filename(".latexmkrc").startswith(".")
        assert not _safe_filename("../.latexmkrc").startswith(".")

    def test_safe_filename_normal(self):
        assert _safe_filename("chapter_1.tex") == "chapter_1.tex"
        assert _safe_filename("figure-01.png") == "figure-01.png"

    def test_detect_main_named_main(self):
        d = get_session_dir(create_session())
        (d / "main.tex").write_text(r"\documentclass{article}\begin{document}\end{document}")
        (d / "other.tex").write_text("x")
        assert detect_main_tex(d) == "main.tex"

    def test_detect_main_by_documentclass(self):
        d = get_session_dir(create_session())
        (d / "preamble.tex").write_text(r"\newcommand{\foo}{bar}")
        (d / "doc.tex").write_text(r"\documentclass{article}\begin{document}Hi\end{document}")
        assert detect_main_tex(d) == "doc.tex"

    def test_detect_main_none(self):
        d = get_session_dir(create_session())
        (d / "readme.txt").write_text("hello")
        assert detect_main_tex(d) is None

    def test_zip_extraction_basic(self):
        d = get_session_dir(create_session())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", r"\documentclass{article}\begin{document}Hi\end{document}")
            zf.writestr("refs.bib", "@article{x,title={X}}")
        extracted = _extract_zip(buf.getvalue(), d)
        assert "main.tex" in extracted and (d / "main.tex").exists()

    def test_zip_slip_blocked(self):
        d = get_session_dir(create_session())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", "ok")
            zf.writestr(zipfile.ZipInfo("../../../evil.tex"), "malicious")
        # zip-slip entries are sanitised to a safe name (never escape the session).
        _extract_zip(buf.getvalue(), d)
        assert not (d.parent.parent / "evil.tex").exists()

    def test_zip_extensionless_rejected(self):
        """The old whitelist let extensionless files through (.latexmkrc RCE)."""
        d = get_session_dir(create_session())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", "ok")
            zf.writestr("latexmkrc", "system('calc.exe');")   # no extension
        extracted = _extract_zip(buf.getvalue(), d)
        assert "main.tex" in extracted
        assert not (d / "latexmkrc").exists()

    def test_zip_bomb_capped(self, monkeypatch):
        from fastapi import HTTPException
        import backend.file_manager as fm
        monkeypatch.setattr(fm, "MAX_EXTRACTED_SIZE_BYTES", 100)
        d = get_session_dir(create_session())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("big.txt", "x" * 5000)  # uncompressed size > cap
        with pytest.raises(HTTPException) as exc:
            _extract_zip(buf.getvalue(), d)
        assert exc.value.status_code == 413


# ════════════════════════════════════════════════════════════════════════════
# GROUP 3 – Compiler internals (no LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestCompilerInternals:

    def test_shell_escape_disabled_by_default(self):
        """Both compile paths must pass -no-shell-escape by default."""
        assert "-no-shell-escape" in _latexmk_cmd("main.tex", "pdflatex")
        assert "-no-shell-escape" in _engine_cmd("pdflatex", "main.tex")
        assert "-shell-escape" not in _engine_cmd("pdflatex", "main.tex")

    def test_shell_escape_opt_in(self, monkeypatch):
        import backend.compiler as compiler
        monkeypatch.setattr(compiler, "ALLOW_SHELL_ESCAPE", True)
        assert "-shell-escape" in compiler._engine_cmd("pdflatex", "main.tex")
        assert "-no-shell-escape" not in compiler._engine_cmd("pdflatex", "main.tex")

    def test_shell_escape_per_compile_override(self):
        """The UI checkbox enables shell-escape for one compile without
        changing the (safe) global default."""
        assert "-shell-escape" in _engine_cmd("pdflatex", "main.tex", True)
        assert "-shell-escape" in _latexmk_cmd("main.tex", "pdflatex", True)
        # explicit False still wins over anything else
        assert "-no-shell-escape" in _engine_cmd("pdflatex", "main.tex", False)
        assert "-no-shell-escape" in _latexmk_cmd("main.tex", "pdflatex", False)

    @pytest.mark.parametrize("engine,flag", [
        ("pdflatex", "-pdf"), ("xelatex", "-xelatex"), ("lualatex", "-lualatex"),
    ])
    def test_latexmk_engine_flags(self, engine, flag):
        assert flag in _latexmk_cmd("main.tex", engine)

    def test_build_env_confines_file_access(self):
        env = _build_env(Path("/tmp/ws"))
        assert env["openin_any"] == "p" and env["openout_any"] == "p"
        assert "TEXINPUTS" in env and "//" in env["TEXINPUTS"]

    def test_check_latex_available_shape(self):
        status = check_latex_available()
        assert "pdflatex" in status
        for info in status.values():
            assert "available" in info and "path" in info

    def test_installable_missing_files_classification(self):
        """The on-demand installer must install package resources but never a
        user asset (figure) or a forgotten \\input .tex."""
        from backend.compiler import _installable_missing_files
        log = (
            "! LaTeX Error: File `listingsutf8.sty' not found.\n"
            "! LaTeX Error: File `IEEEtran.bst' not found.\n"
            "! LaTeX Error: File `pgfsys-pdftex.def' not found.\n"
            "! LaTeX Error: File `chapter1.tex' not found.\n"
            "! Package pdftex.def Error: File `logo.png' not found.\n"
            "I couldn't open style file achemso.bst\n"
        )
        got = _installable_missing_files(log)
        assert "listingsutf8.sty" in got and "IEEEtran.bst" in got
        assert "pgfsys-pdftex.def" in got and "achemso.bst" in got
        assert "chapter1.tex" not in got   # user \input, not a package
        assert "logo.png" not in got       # user asset, not a package

    def test_resolve_binary_finds_bat(self, tmp_path, monkeypatch):
        """Regression: TinyTeX ships tlmgr as tlmgr.bat; the resolver must find
        .bat/.cmd scripts, not only .exe."""
        import backend.compiler as compiler
        (tmp_path / "tlmgr.bat").write_text("@echo off")
        monkeypatch.setattr(compiler, "LATEX_BIN_PATH", str(tmp_path))
        assert compiler._resolve_binary("tlmgr").endswith("tlmgr.bat")


# ════════════════════════════════════════════════════════════════════════════
# GROUP 4 – Compilation scenarios (require LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestCompilation:

    @requires_latex
    def test_simple_compile_produces_fresh_pdf(self):
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex")
            assert result.success, result.summary
            assert result.pdf_path and result.pdf_path.exists()
            assert result.pdf_path.read_bytes()[:4] == b"%PDF"
        finally:
            delete_session(sid)

    @requires_latex
    def test_failed_recompile_does_not_report_stale_success(self):
        """Regression (C4): after a good compile, a broken recompile must FAIL
        even though the previous PDF is still on disk."""
        sid, d = _setup_session_from_fixture("simple")
        try:
            assert compile_project(d, "main.tex").success
            # Introduce a fatal (no-output) error.
            (d / "main.tex").write_text(r"\documentclassarticle\begin{document}\end{document}")
            result = compile_project(d, "main.tex")
            assert not result.success
            assert result.pdf_path is None
        finally:
            delete_session(sid)

    @requires_latex
    def test_with_images_embeds_png(self):
        sid, d = _setup_session_from_fixture("with_images")
        try:
            result = compile_project(d, "main.tex")
            assert result.success, result.summary
            assert not any("not found" in e.message.lower() for e in result.parsed_log.errors)
        finally:
            delete_session(sid)

    @requires_latex
    def test_multi_file_input(self):
        sid, d = _setup_session_from_fixture("multi_file")
        try:
            assert compile_project(d, "main.tex").success
        finally:
            delete_session(sid)

    @requires_latex
    def test_subdir_resource_resolved_via_texinputs(self):
        """A .sty in a sub-folder, referenced by bare name, must resolve."""
        sid = create_session()
        d = get_session_dir(sid)
        (d / "sty").mkdir()
        (d / "sty" / "mypkg.sty").write_text(
            r"\ProvidesPackage{mypkg}\newcommand{\hello}{Hi there}")
        (d / "main.tex").write_text(
            r"\documentclass{article}\usepackage{mypkg}\begin{document}\hello\end{document}")
        try:
            result = compile_project(d, "main.tex")
            assert result.success, result.summary
        finally:
            delete_session(sid)

    @requires_latex
    def test_broken_document_surfaces_errors(self):
        sid, d = _setup_session_from_fixture("broken")
        try:
            result = compile_project(d, "main.tex")
            assert result.parsed_log is not None
            assert result.parsed_log.has_errors
            assert len(result.parsed_log.errors) >= 1
        finally:
            delete_session(sid)

    def test_missing_main_file_graceful(self):
        """Pure-Python path – runs even without LaTeX."""
        sid = create_session()
        d = get_session_dir(sid)
        try:
            result = compile_project(d, "nonexistent.tex")
            assert not result.success
            assert "not found" in result.summary.lower()
        finally:
            delete_session(sid)

    @requires_latex
    @pytest.mark.parametrize("engine", ["pdflatex", "xelatex", "lualatex"])
    def test_each_engine_produces_pdf(self, engine):
        if not _engine_available(engine):
            pytest.skip(f"{engine} not installed")
        sid, d = _setup_session_from_fixture("simple")
        try:
            result = compile_project(d, "main.tex", engine=engine)
            assert result.success, f"{engine}: {result.summary}"
            assert result.pdf_path.read_bytes()[:4] == b"%PDF"
        finally:
            delete_session(sid)

    @requires_latex
    def test_with_bibtex_builds_bibliography(self):
        sid, d = _setup_session_from_fixture("with_bibtex")
        try:
            result = compile_project(d, "main.tex")
            assert result.parsed_log is not None
            if result.success:
                # No unresolved citations should remain after the multi-pass.
                assert not any("Citation" in w.message and "undefined" in w.message.lower()
                               for w in result.parsed_log.warnings)
        finally:
            delete_session(sid)

    @requires_latex
    def test_with_biber_biblatex(self):
        if not check_latex_available().get("biber", {}).get("available"):
            pytest.skip("biber not installed")
        sid, d = _setup_session_from_fixture("with_biber")
        try:
            result = compile_project(d, "main.tex")
            assert result.parsed_log is not None
            if result.success:
                assert not any("Citation" in w.message and "undefined" in w.message.lower()
                               for w in result.parsed_log.warnings)
        finally:
            delete_session(sid)

    @requires_latex
    def test_custom_class(self):
        sid, d = _setup_session_from_fixture("custom_class")
        try:
            assert compile_project(d, "main.tex").success
        finally:
            delete_session(sid)

    @requires_latex
    def test_timeout_terminates(self):
        sid = create_session()
        d = get_session_dir(sid)
        (d / "main.tex").write_text(
            r"\documentclass{article}\begin{document}\loop\iftrue\repeat\end{document}")
        try:
            result = compile_project(d, "main.tex", timeout=5)
            assert not result.success
        finally:
            delete_session(sid)

    def test_concurrent_sessions_keep_content_isolated(self):
        """Real threads writing distinct content must not cross-contaminate."""
        sids = [create_session() for _ in range(4)]
        errors = []

        def worker(i, sid):
            try:
                d = get_session_dir(sid)
                (d / "main.tex").write_text(f"session-{i}-content")
                assert (d / "main.tex").read_text() == f"session-{i}-content"
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i, s)) for i, s in enumerate(sids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert not errors
            assert len({get_session_dir(s) for s in sids}) == 4
        finally:
            for s in sids:
                delete_session(s)
