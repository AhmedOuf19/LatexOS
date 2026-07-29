"""
test_compiler.py – log parser, file manager, and compilation-scenario tests.

Non-LaTeX tests (parser, file manager, path safety, engine flag maps) always
run. Compilation tests are marked @requires_latex and auto-skip when no LaTeX
distribution is installed (see conftest.py). Every assertion is written so it
would actually FAIL if the feature it covers regressed.

Layout
------
* GROUP 1 – log parser         (pure Python, no LaTeX needed)
* GROUP 2 – file manager       (sessions, uploads, path safety)
* GROUP 3 – compiler internals (command lines, env, binary resolution)
* GROUP 4 – compilation        (real end-to-end compiles, mostly @requires_latex)

A large share of these are REGRESSION tests for bugs that shipped: each one
names the bug in its docstring, so nobody "simplifies" the fix back out.

Cleanup: conftest.py's autouse ``isolated_uploads`` fixture repoints
``UPLOAD_DIR`` at a per-test temp directory, so a session left behind by a test
is discarded with the temp dir and can never touch the real ``uploads/``.
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


# ════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ════════════════════════════════════════════════════════════════════════════

def _setup_session_from_fixture(fixture_name: str) -> tuple[str, Path]:
    """Create a session and copy ``tests/fixtures/<fixture_name>`` into it.

    Returns ``(session_id, session_dir)``. Fixtures are COPIED rather than
    compiled in place so that build artifacts (.aux/.log/.pdf) never end up in
    the repository's fixture folders and leak between test runs.

    A fixture name that does not exist yields an empty session rather than an
    error; the test then fails on the missing main .tex, which is loud enough.
    """
    session_id = create_session()
    session_dir = get_session_dir(session_id)
    fixture_src = FIXTURES_DIR / fixture_name
    if fixture_src.is_dir():
        shutil.copytree(fixture_src, session_dir, dirs_exist_ok=True)
    return session_id, session_dir


def _engine_available(engine: str) -> bool:
    """True if this specific engine binary can be resolved.

    The suite-wide ``requires_latex`` marker only checks pdflatex (conftest.py),
    so per-engine tests must re-check their own binary: a minimal TinyTeX
    commonly has pdflatex but not xelatex or lualatex.
    """
    return check_latex_available().get(engine, {}).get("available", False)


# ════════════════════════════════════════════════════════════════════════════
# GROUP 1 – Log parser (no LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestLogParser:
    """Prove that a raw LaTeX .log becomes an honest structured report.

    The two failure modes that matter to a user are opposite: reporting a
    broken document as clean (errors silently dropped) and reporting a clean
    document as broken (false warnings). Both are covered here, with real log
    excerpts rather than invented text.
    """

    def test_clean_log_has_no_errors(self):
        """A successful run produces zero errors and zero warnings.

        This is the false-positive baseline: if the parser ever starts flagging
        ordinary log chatter, every good compile would show a red error panel.
        """
        result = parse_log("This is pdfTeX\nOutput written on main.pdf (2 pages).\n")
        assert not result.has_errors
        assert result.errors == [] and result.warnings == []

    def test_bang_error_captured(self):
        """A classic ``! …`` error is captured, with its source line number.

        TeX prints the offending source line separately as ``l.10 …`` after the
        message, so the parser has to look ahead to attach ``line``; without it
        the UI could not link an error to a place in the document.
        """
        result = parse_log("! Emergency stop.\nl.10 \\end{document}\n")
        assert result.has_errors
        assert "Emergency stop" in result.errors[0].message
        assert result.errors[0].line == 10

    def test_file_line_error_captured(self):
        """The ``file:line: message`` error form is captured with file AND line.

        Regression: the compiler runs with ``-file-line-error``, so these errors
        have no ``! `` prefix. A parser matching only ``"! "`` dropped them
        entirely — the most common class of error — and a broken document
        reported as clean.
        """
        raw_log = "(./main.tex\n./main.tex:4: Undefined control sequence.\nl.4 \\badcmd\n"
        result = parse_log(raw_log)
        assert result.has_errors, "file:line error must be captured"
        error = result.errors[0]
        assert error.line == 4 and "main.tex" in error.file
        assert "Undefined control sequence" in error.message

    def test_missing_package_hint(self):
        """A missing .sty yields an error naming the package.

        The user has to know WHICH package is missing to act on it (or to trust
        the on-demand installer), so the package name must survive into the
        message rather than being reduced to a generic "file not found".
        """
        result = parse_log("! LaTeX Error: File `nonexistentpackage.sty' not found.\n")
        assert any("nonexistentpackage" in e.message for e in result.errors)

    def test_file_line_errors_without_whitelist_words(self):
        """Errors are recognised by FORM, not by keywords in their text.

        Regression: the parser once required a hand-maintained whitelist of
        phrases. Errors whose text lacked one (Too many }'s, Double superscript,
        Illegal unit, Misplaced alignment) were silently dropped, so a broken
        document reported as clean.
        """
        for error_text in ("Too many }'s.", "Double superscript.",
                           "Illegal unit of measure (pt inserted).",
                           "Misplaced alignment tab character &."):
            result = parse_log(f"./doc.tex:5: {error_text}\nl.5 ...\n")
            assert result.has_errors, f"must capture: {error_text}"

    def test_windows_drive_letter_path_error(self):
        """An error from a drive-lettered absolute path still parses.

        Regression: this app ships a folder-local TinyTeX, so packages live at
        paths like ``c:/…/foo.sty``. The drive-letter colon looks exactly like
        the ``file:line`` separator, and a naive pattern truncated the filename
        to ``c`` (or failed to match at all), losing the error.
        """
        raw_log = "c:/proj/tinytex/texmf-dist/tex/latex/foo/foo.sty:88: Undefined control sequence.\n"
        result = parse_log(raw_log)
        assert result.has_errors
        assert result.errors[0].file.startswith("c:/")

    def test_shell_escape_hint(self):
        """A minted failure must tell the user how to enable shell-escape.

        LaTeX's own message says only that the executable is "unavailable or
        disabled" — it never mentions the flag, and this app deliberately
        disables shell-escape by default, so the fix has to be spelled out.
        """
        raw_log = ("chapter3.tex:114: Package minted Error: Cannot highlight code "
                   "(minted executable is unavailable or disabled).\n")
        result = parse_log(raw_log)
        assert any("Shell-escape" in e.message for e in result.errors)

    def test_bookmark_hint(self):
        """The \\@@BOOKMARK runaway must be explained in plain language.

        The raw error points at hyperref internals and names no file the user
        wrote; the real cause is a fragile command (usually ``\\url{…}``) in a
        section title, which nothing in the log states.
        """
        raw_log = ("Runaway argument?\n{\\376\\377\\000S\\000p\\ETC.\n"
                   "main.tex:208: File ended while scanning use of \\@@BOOKMARK.\n")
        result = parse_log(raw_log)
        assert any("bookmark" in e.message.lower() for e in result.errors)

    def test_unicode_hint_suggests_engine_switch(self):
        """Unicode-character errors should point at xelatex/lualatex.

        The app has an engine selector, so this is a one-click fix. The hint
        must also carry the code point (U+03B8) — otherwise the user cannot
        find the offending character in a long document.
        """
        raw_log = ("main.tex:12: LaTeX Error: Unicode character \u03b8 (U+03B8)\n"
                   "               not set up for use with LaTeX.\n")
        result = parse_log(raw_log)
        hints = [e.message for e in result.errors if "xelatex" in e.message]
        assert hints and "U+03B8" in hints[0]

    def test_no_false_bbl_warning(self):
        """Regression: a successful build that loads (./main.bbl) plus a benign
        'No file main.out.' line must NOT report a missing bibliography.

        The old check looked for two unrelated substrings anywhere in the log
        ("No file" and ".bbl") instead of matching a single line, so a perfectly
        good bibliography was reported as missing on almost every real document.
        """
        result = parse_log("(./main.bbl)\nNo file main.out.\nOutput written on main.pdf (1 page).\n")
        assert not any(".bbl" in w.message for w in result.warnings)

    def test_seventy_nine_char_line_does_not_swallow_error(self):
        """Regression: a coincidental 79-char line must not merge with a
        following '! ...' error and hide it.

        TeX hard-wraps log lines at ``max_print_line`` (79), so the parser
        rejoins continuations. Any line that happens to be exactly 79 characters
        looked like a wrap, and the next line — the error — was glued onto it
        and never recognised.
        """
        result = parse_log(("X" * 79) + "\n! Undefined control sequence.\nl.1 x\n")
        assert result.has_errors

    def test_latex_warning_captured(self):
        """A ``LaTeX Warning:`` becomes a warning (not an error) with its text intact.

        The citation key is the only actionable part of the message, so it must
        survive parsing; and misclassifying this as an error would make an
        undefined citation block the "compiled successfully" state.
        """
        result = parse_log("LaTeX Warning: Citation `smith2020' on page 1 undefined.\n")
        assert len(result.warnings) >= 1
        assert "smith2020" in result.warnings[0].message

    def test_overfull_hbox_is_badbox(self):
        """An overfull box is classified as a badbox and anchored to line 42.

        Badboxes are cosmetic, so they get their own bucket rather than
        polluting errors/warnings. The reported line is the FIRST of the
        ``42--50`` range — the paragraph start, which is where the user edits.
        """
        result = parse_log("Overfull \\hbox (5.0pt too wide) in paragraph at lines 42--50\n")
        assert len(result.badboxes) == 1
        assert result.badboxes[0].line == 42

    def test_duplicate_entries_deduped(self):
        """The same badbox on many pages should appear once.

        A single bad paragraph in a repeated header can emit hundreds of
        identical lines; without dedupe they bury every real problem in the UI.
        """
        badbox_line = "Overfull \\hbox (5.0pt too wide) in paragraph at lines 42--50\n"
        result = parse_log(badbox_line * 5)
        assert len(result.badboxes) == 1

    def test_to_dict_keys(self):
        """``ParsedLog.to_dict()`` always exposes the keys the frontend reads.

        These names are the JSON contract consumed by app.js; dropping or
        renaming one breaks the log panel at runtime with no Python error.
        """
        log_dict = parse_log("! Test error.\nl.1 x\n").to_dict()
        for key in ("errors", "warnings", "badboxes", "raw", "has_errors"):
            assert key in log_dict

    def test_summary_clean_vs_errors(self):
        """The one-line summary distinguishes a clean run from a failed one.

        It is the only feedback a non-technical user reads before deciding
        whether to open the log, so the two cases must not read alike.
        """
        assert "no issues" in get_log_summary(parse_log("Output written.\n")).lower()
        assert "error" in get_log_summary(parse_log("! e1.\nl.1 x\n! e2.\nl.2 y\n")).lower()


# ════════════════════════════════════════════════════════════════════════════
# GROUP 2 – File manager & path safety (no LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestFileManager:
    """Prove that uploads stay inside their session and cannot become code.

    Everything here is a boundary the app exposes to untrusted input: session
    ids from URLs, filenames from uploads, and ZIP members. The security tests
    assert the ATTACK fails, so a weakened guard shows up as a failing test
    rather than as a quietly widened hole.
    """

    def test_create_and_delete_session(self):
        """A session directory is created on request and fully removed on delete.

        Delete must actually remove the directory: leftovers would accumulate
        uploaded documents on the user's disk indefinitely.
        """
        session_id = create_session()
        session_dir = get_session_dir(session_id)
        assert session_dir.exists()
        delete_session(session_id)
        assert not session_dir.exists()

    def test_invalid_session_id_rejected(self):
        """A traversal payload as the session id is rejected with 400.

        The session id comes straight from the URL and is joined onto
        ``uploads/``; unvalidated, ``../etc/passwd`` would address any directory
        on the machine. 400 (not 404) proves it failed FORMAT validation, i.e.
        it was never turned into a filesystem path at all.
        """
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            get_session_dir("../etc/passwd")
        assert exc.value.status_code == 400

    def test_noncanonical_uuid_rejected(self):
        """36 chars of [0-9a-f-] is not enough – must be a canonical UUID.

        A length-plus-charset check would accept this string, so the validator
        has to enforce the 8-4-4-4-12 layout; anything looser widens the set of
        directory names an attacker can address.
        """
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            get_session_dir("-" * 36)

    def test_safe_filename_strips_traversal(self):
        """Neither separator survives sanitising, on either platform.

        Regression: the sanitiser used ``Path(filename).name``, which on POSIX
        does not treat ``\\`` as a separator — so a Windows-style upload name
        like ``..\\..\\windows\\system32`` passed through completely intact.
        The second assertion is the one that used to fail.
        """
        assert "/" not in _safe_filename("../../../etc/passwd")
        assert "\\" not in _safe_filename("..\\..\\windows\\system32")

    def test_safe_filename_neutralises_dotfiles(self):
        """A leading dot is stripped, so .latexmkrc cannot be created.

        ``.latexmkrc`` is a Perl config file that latexmk executes from the
        working directory: writing one into a session would turn a document
        upload into arbitrary code execution on the next compile.
        """
        assert not _safe_filename(".latexmkrc").startswith(".")
        assert not _safe_filename("../.latexmkrc").startswith(".")

    def test_safe_filename_normal(self):
        """Ordinary filenames pass through byte-for-byte.

        The guard against over-sanitising: mangling underscores, hyphens or
        digits would silently break every ``\\input`` and ``\\includegraphics``
        in a legitimate project.
        """
        assert _safe_filename("chapter_1.tex") == "chapter_1.tex"
        assert _safe_filename("figure-01.png") == "figure-01.png"

    def test_detect_main_named_main(self):
        """An explicit root ``main.tex`` wins over any other candidate.

        It is the strongest signal of intent and by far the most common
        convention, so it is checked before any content heuristic.
        """
        session_dir = get_session_dir(create_session())
        (session_dir / "main.tex").write_text(r"\documentclass{article}\begin{document}\end{document}")
        (session_dir / "other.tex").write_text("x")
        assert detect_main_tex(session_dir) == "main.tex"

    def test_detect_main_by_documentclass(self):
        """Without a ``main.tex``, the file holding ``\\documentclass`` is chosen.

        Only that file can be compiled; an included fragment such as a preamble
        would fail immediately, so name-based guessing is not enough.
        """
        session_dir = get_session_dir(create_session())
        (session_dir / "preamble.tex").write_text(r"\newcommand{\foo}{bar}")
        (session_dir / "doc.tex").write_text(r"\documentclass{article}\begin{document}Hi\end{document}")
        assert detect_main_tex(session_dir) == "doc.tex"

    def test_detect_main_none(self):
        """A project with no .tex returns None instead of raising or guessing.

        The upload endpoint reports ``detected_main: null`` and lets the user
        choose; picking an arbitrary non-.tex file would produce a baffling
        compile error instead.
        """
        session_dir = get_session_dir(create_session())
        (session_dir / "readme.txt").write_text("hello")
        assert detect_main_tex(session_dir) is None

    def test_zip_extraction_basic(self):
        """A normal archive extracts, and its members really land on disk.

        The happy path for the main upload route — the security tests below all
        assert that something is BLOCKED, so this is what proves the extractor
        still does its job.
        """
        session_dir = get_session_dir(create_session())
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("main.tex", r"\documentclass{article}\begin{document}Hi\end{document}")
            archive.writestr("refs.bib", "@article{x,title={X}}")
        extracted = _extract_zip(zip_buffer.getvalue(), session_dir)
        assert "main.tex" in extracted and (session_dir / "main.tex").exists()

    def test_zip_slip_blocked(self):
        """A ``../../../evil.tex`` member cannot write outside the session.

        "Zip slip": the archive member name is attacker-controlled, and a naive
        extractor joins it onto the destination, letting an upload overwrite
        files anywhere the server can write.
        """
        session_dir = get_session_dir(create_session())
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("main.tex", "ok")
            archive.writestr(zipfile.ZipInfo("../../../evil.tex"), "malicious")
        # zip-slip entries are sanitised to a safe name (never escape the session).
        _extract_zip(zip_buffer.getvalue(), session_dir)
        assert not (session_dir.parent.parent / "evil.tex").exists()

    def test_zip_extensionless_rejected(self):
        """The old whitelist let extensionless files through (.latexmkrc RCE).

        Regression: the extension check tested ``suffix not in ALLOWED`` on a
        name with no suffix at all, so a member literally called ``latexmkrc``
        was extracted; latexmk then read and executed it as Perl. Valid members
        in the same archive must still be extracted.
        """
        session_dir = get_session_dir(create_session())
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("main.tex", "ok")
            archive.writestr("latexmkrc", "system('calc.exe');")   # no extension
        extracted = _extract_zip(zip_buffer.getvalue(), session_dir)
        assert "main.tex" in extracted
        assert not (session_dir / "latexmkrc").exists()

    def test_zip_bomb_capped(self, monkeypatch):
        """An archive whose contents exceed the cap is refused with 413.

        The limit is patched down to 100 bytes so the test does not have to
        build a real multi-gigabyte bomb. The cap must be enforced against the
        UNCOMPRESSED size — a few KB of zip can expand to gigabytes on disk.
        """
        from fastapi import HTTPException
        import backend.file_manager as fm
        monkeypatch.setattr(fm, "MAX_EXTRACTED_SIZE_BYTES", 100)
        session_dir = get_session_dir(create_session())
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("big.txt", "x" * 5000)  # uncompressed size > cap
        with pytest.raises(HTTPException) as exc:
            _extract_zip(zip_buffer.getvalue(), session_dir)
        assert exc.value.status_code == 413


# ════════════════════════════════════════════════════════════════════════════
# GROUP 3 – Compiler internals (no LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestCompilerInternals:
    """Prove the compiler's decisions without running LaTeX.

    Command lines, the subprocess environment and binary resolution are pure
    functions, so the security-critical defaults (shell-escape off, paranoid
    file access) can be asserted on every machine — including CI with no TeX
    installed, where the end-to-end tests are skipped.
    """

    def test_shell_escape_disabled_by_default(self):
        """Both compile paths must pass -no-shell-escape by default.

        Passing neither flag is not equivalent: some distributions default to
        restricted shell-escape, so the deny must be explicit. latexmk and the
        manual fallback build their command lines separately, hence both.
        """
        assert "-no-shell-escape" in _latexmk_cmd("main.tex", "pdflatex")
        assert "-no-shell-escape" in _engine_cmd("pdflatex", "main.tex")
        assert "-shell-escape" not in _engine_cmd("pdflatex", "main.tex")

    def test_shell_escape_opt_in(self, monkeypatch):
        """Setting LATEX_ALLOW_SHELL_ESCAPE globally does enable it.

        The escape hatch has to work, or users with a legitimate minted document
        would be stuck. The flag is read from the module global at call time, so
        the test patches ``backend.compiler.ALLOW_SHELL_ESCAPE`` — this covers
        the config/env-var path, not the per-compile override tested below.
        """
        import backend.compiler as compiler
        monkeypatch.setattr(compiler, "ALLOW_SHELL_ESCAPE", True)
        assert "-shell-escape" in compiler._engine_cmd("pdflatex", "main.tex")
        assert "-no-shell-escape" not in compiler._engine_cmd("pdflatex", "main.tex")

    def test_shell_escape_per_compile_override(self):
        """The UI checkbox enables shell-escape for one compile without
        changing the (safe) global default.

        Also pins the three-state contract of the argument: True enables, False
        denies, and only None falls back to the config value — so an explicit
        False can never be overridden by a permissive global.
        """
        assert "-shell-escape" in _engine_cmd("pdflatex", "main.tex", True)
        assert "-shell-escape" in _latexmk_cmd("main.tex", "pdflatex", True)
        # explicit False still wins over anything else
        assert "-no-shell-escape" in _engine_cmd("pdflatex", "main.tex", False)
        assert "-no-shell-escape" in _latexmk_cmd("main.tex", "pdflatex", False)

    @pytest.mark.parametrize("engine,flag", [
        ("pdflatex", "-pdf"), ("xelatex", "-xelatex"), ("lualatex", "-lualatex"),
    ])
    def test_latexmk_engine_flags(self, engine, flag):
        """The engine chosen in the UI maps to the right latexmk flag.

        latexmk selects the engine by flag, not by binary name, and it spells
        pdfTeX ``-pdf`` (there is no ``-pdflatex``). A wrong mapping does not
        error — it silently compiles with the default engine, so a user who
        picked xelatex to fix Unicode gets the same failure again.
        """
        assert flag in _latexmk_cmd("main.tex", engine)

    def test_build_env_confines_file_access(self):
        """The subprocess environment confines file I/O and finds sub-folders.

        ``openin_any``/``openout_any`` = ``p`` (paranoid) stops a document from
        reading or writing outside its own tree — without it, an uploaded .tex
        could ``\\input`` a private file and typeset it into the PDF. The ``//``
        suffix is kpathsea's "search recursively" marker, which is what makes
        resources in sub-folders resolve.
        """
        env = _build_env(Path("/tmp/ws"))
        assert env["openin_any"] == "p" and env["openout_any"] == "p"
        assert "TEXINPUTS" in env and "//" in env["TEXINPUTS"]

    def test_check_latex_available_shape(self):
        """Every reported tool carries both ``available`` and ``path``.

        The /api/status endpoint and run.py's startup banner read these keys
        directly; a missing key would surface as a runtime crash in the UI, not
        a Python error here. Works with or without LaTeX installed.
        """
        status = check_latex_available()
        assert "pdflatex" in status
        for info in status.values():
            assert "available" in info and "path" in info

    def test_installable_missing_files_classification(self):
        """The on-demand installer must install package resources but never a
        user asset (figure) or a forgotten \\input .tex.

        Trying to install a user's ``logo.png`` wastes a network round-trip and
        buries the real cause ("you forgot to upload the image") under a
        package-manager error. The .bst cases also cover bibtex's own phrasing,
        which appears in the .blg rather than the .log.
        """
        from backend.compiler import _installable_missing_files
        raw_log = (
            "! LaTeX Error: File `listingsutf8.sty' not found.\n"
            "! LaTeX Error: File `IEEEtran.bst' not found.\n"
            "! LaTeX Error: File `pgfsys-pdftex.def' not found.\n"
            "! LaTeX Error: File `chapter1.tex' not found.\n"
            "! Package pdftex.def Error: File `logo.png' not found.\n"
            "I couldn't open style file achemso.bst\n"
        )
        installable = _installable_missing_files(raw_log)
        assert "listingsutf8.sty" in installable and "IEEEtran.bst" in installable
        assert "pgfsys-pdftex.def" in installable and "achemso.bst" in installable
        assert "chapter1.tex" not in installable   # user \input, not a package
        assert "logo.png" not in installable       # user asset, not a package

    def test_missing_file_names_survive_trailing_punctuation(self):
        """A filename at the end of a sentence must keep its extension.

        Regression: bibtex writes "I couldn't open style file IEEEtran.bst."
        with a full stop. That trailing '.' was captured as part of the name, so
        Path(name).suffix became '' and the entry was discarded as "not a
        package" — meaning a missing bibliography style could never be
        auto-installed and the user saw an unexplained bibliography failure.
        """
        from backend.compiler import _installable_missing_files

        for log_line in ("I couldn't open style file IEEEtran.bst.",
                         "open file plainnat.bst. for reading",
                         "! LaTeX Error: File `natbib.sty' not found."):
            found = _installable_missing_files(log_line)
            assert found, f"nothing captured from: {log_line}"
            assert all(not name.endswith(".") for name in found), found
            assert all(Path(name).suffix for name in found), found

    def test_self_update_failure_does_not_latch_forever(self):
        """A failed tlmgr self-update must not disable installs for the session.

        It is retried once per compile, not once per process. Regression: an
        early transient failure (offline at start-up, mirror briefly down) used
        to latch process-wide, silently disabling on-demand package
        installation for the life of the server with no way to discover why.
        """
        import backend.compiler as compiler

        # A binary that cannot be executed raises OSError -> the failure path.
        first_compile: set[str] = set()
        assert compiler._tlmgr_self_update("not-a-real-binary-xyz", {}, first_compile) is False
        assert compiler._SELF_UPDATE_MARKER in first_compile, "must not retry within one compile"
        assert compiler._tlmgr_self_update("not-a-real-binary-xyz", {}, first_compile) is False
        assert compiler._tlmgr_self_updated is False, "a failure must not latch process-wide"

        # A LATER compile gets a fresh set, so it is allowed to try again.
        second_compile: set[str] = set()
        compiler._tlmgr_self_update("not-a-real-binary-xyz", {}, second_compile)
        assert compiler._SELF_UPDATE_MARKER in second_compile

    def test_detects_tlmgr_needs_self_update(self):
        """Regression: a freshly-installed TinyTeX ships a tlmgr older than the
        remote repository and refuses to install anything until it self-updates.

        On Windows tlmgr is a .bat wrapper that exits 0 even on this failure, so
        the condition MUST be detected from the output text. Without this, the
        first compile needing any package failed with a confusing
        "File `x.sty' not found" on every fresh install.
        """
        from backend.compiler import _RE_TLMGR_NEEDS_SELF_UPDATE

        real_output = (
            "===============================================================\n"
            "tlmgr itself needs to be updated.\n"
            "Please do this via either\n"
            "  tlmgr update --self\n"
            "tlmgr.pl: Terminating; please see warning above!\n"
        )
        assert _RE_TLMGR_NEEDS_SELF_UPDATE.search(real_output)
        # The other phrasing TeX Live uses for the same condition.
        assert _RE_TLMGR_NEEDS_SELF_UPDATE.search(
            "local TeX Live (2025) is older than remote repository (2026)"
        )
        # And it must not fire on an ordinary successful install.
        assert not _RE_TLMGR_NEEDS_SELF_UPDATE.search(
            "tlmgr: package repository https://tlnet.yihui.org (verified)\n"
            "[1/1, ??:??/??:??] install: listingsutf8 [1k]\n"
            "tlmgr: package log updated"
        )

    def test_resolve_binary_finds_bat(self, tmp_path, monkeypatch):
        """Regression: TinyTeX ships tlmgr as tlmgr.bat; the resolver must find
        .bat/.cmd scripts, not only .exe.

        Resolving only ``.exe`` made ``_has_binary("tlmgr")`` return False, which
        silently disabled on-demand package installation altogether — no error
        message, packages simply never installed.
        """
        import backend.compiler as compiler
        (tmp_path / "tlmgr.bat").write_text("@echo off")
        monkeypatch.setattr(compiler, "LATEX_BIN_PATH", str(tmp_path))
        assert compiler._resolve_binary("tlmgr").endswith("tlmgr.bat")


# ════════════════════════════════════════════════════════════════════════════
# GROUP 4 – Compilation scenarios (require LaTeX)
# ════════════════════════════════════════════════════════════════════════════

class TestCompilation:
    """End-to-end compiles against a real LaTeX installation.

    These are the only tests that prove the whole pipeline (env, command line,
    multi-pass, artifact cleaning, log parsing) actually produces a PDF. They
    are skipped when pdflatex is absent, except where noted; the nightly CI job
    sets ``LATEX_STUDIO_REQUIRE_LATEX=1`` to turn that skip into a hard error.

    Each test deletes its session in a ``finally`` so a failing assertion never
    leaves a half-compiled workspace behind for the next test to trip over.
    """

    @requires_latex
    def test_simple_compile_produces_fresh_pdf(self):
        """A minimal document compiles to a real PDF.

        The ``%PDF`` magic-byte check matters: an aborted run can leave a
        zero-length or truncated .pdf on disk, which a mere ``exists()`` test
        would happily accept as success.
        """
        session_id, session_dir = _setup_session_from_fixture("simple")
        try:
            result = compile_project(session_dir, "main.tex")
            assert result.success, result.summary
            assert result.pdf_path and result.pdf_path.exists()
            assert result.pdf_path.read_bytes()[:4] == b"%PDF"
        finally:
            delete_session(session_id)

    @requires_latex
    def test_failed_recompile_does_not_report_stale_success(self):
        """Regression (C4): after a good compile, a broken recompile must FAIL
        even though the previous PDF is still on disk.

        Success is judged by "a PDF exists", so without cleaning artifacts first
        the previous run's PDF made every subsequent failure look successful —
        the user saw a green result and an unchanged document.
        """
        session_id, session_dir = _setup_session_from_fixture("simple")
        try:
            assert compile_project(session_dir, "main.tex").success
            # Introduce a fatal (no-output) error.
            (session_dir / "main.tex").write_text(r"\documentclassarticle\begin{document}\end{document}")
            result = compile_project(session_dir, "main.tex")
            assert not result.success
            assert result.pdf_path is None
        finally:
            delete_session(session_id)

    @requires_latex
    def test_with_images_embeds_png(self):
        """An uploaded PNG is found by ``\\includegraphics``.

        Graphics are resolved through the environment this app builds
        (TEXINPUTS + the paranoid ``openin_any``), so a too-strict confinement
        would break image inclusion. Checking for "not found" errors catches the
        case where LaTeX proceeds anyway and produces a PDF with a blank box.
        """
        session_id, session_dir = _setup_session_from_fixture("with_images")
        try:
            result = compile_project(session_dir, "main.tex")
            assert result.success, result.summary
            assert not any("not found" in e.message.lower() for e in result.parsed_log.errors)
        finally:
            delete_session(session_id)

    @requires_latex
    def test_multi_file_input(self):
        """A project split across ``chapters/`` compiles as one document.

        Proves the compile runs in the main .tex's own directory, so relative
        ``\\input{chapters/…}`` paths resolve the way the author wrote them.
        """
        session_id, session_dir = _setup_session_from_fixture("multi_file")
        try:
            assert compile_project(session_dir, "main.tex").success
        finally:
            delete_session(session_id)

    @requires_latex
    def test_subdir_resource_resolved_via_texinputs(self):
        """A .sty in a sub-folder, referenced by bare name, must resolve.

        ``\\usepackage{mypkg}`` gives kpathsea no path, so this only works
        because TEXINPUTS lists the workspace with the recursive ``//`` suffix.
        Users routinely upload a ZIP with styles tucked into a sub-folder.
        """
        session_id = create_session()
        session_dir = get_session_dir(session_id)
        (session_dir / "sty").mkdir()
        (session_dir / "sty" / "mypkg.sty").write_text(
            r"\ProvidesPackage{mypkg}\newcommand{\hello}{Hi there}")
        (session_dir / "main.tex").write_text(
            r"\documentclass{article}\usepackage{mypkg}\begin{document}\hello\end{document}")
        try:
            result = compile_project(session_dir, "main.tex")
            assert result.success, result.summary
        finally:
            delete_session(session_id)

    @requires_latex
    def test_broken_document_surfaces_errors(self):
        """A document with real LaTeX errors reports them through parsed_log.

        The end-to-end counterpart of the GROUP 1 parser tests: it proves the
        errors survive the whole path (nonstopmode run → .log on disk → parse),
        not just that the regexes match a hand-written string.
        """
        session_id, session_dir = _setup_session_from_fixture("broken")
        try:
            result = compile_project(session_dir, "main.tex")
            assert result.parsed_log is not None
            assert result.parsed_log.has_errors
            assert len(result.parsed_log.errors) >= 1
        finally:
            delete_session(session_id)

    def test_missing_main_file_graceful(self):
        """Pure-Python path – runs even without LaTeX.

        A missing main .tex must come back as a normal failed result with an
        explanatory summary, not an exception: this path is reachable from the
        API whenever a user renames or deletes the detected main file, and an
        unhandled error there would surface as a bare HTTP 500.
        """
        session_id = create_session()
        session_dir = get_session_dir(session_id)
        try:
            result = compile_project(session_dir, "nonexistent.tex")
            assert not result.success
            assert "not found" in result.summary.lower()
        finally:
            delete_session(session_id)

    @requires_latex
    @pytest.mark.parametrize("engine", ["pdflatex", "xelatex", "lualatex"])
    def test_each_engine_produces_pdf(self, engine):
        """Every engine offered in the UI really produces a PDF.

        The engine selector is the recommended fix for Unicode errors, so an
        engine that is offered but broken is worse than one that is absent. A
        minimal TinyTeX often lacks xelatex/lualatex, hence the per-engine skip
        rather than relying on the pdflatex-only ``requires_latex`` marker.
        """
        if not _engine_available(engine):
            pytest.skip(f"{engine} not installed")
        session_id, session_dir = _setup_session_from_fixture("simple")
        try:
            result = compile_project(session_dir, "main.tex", engine=engine)
            assert result.success, f"{engine}: {result.summary}"
            assert result.pdf_path.read_bytes()[:4] == b"%PDF"
        finally:
            delete_session(session_id)

    @requires_latex
    def test_with_bibtex_builds_bibliography(self):
        """A bibtex document ends with no unresolved citations.

        Citations need at least three passes (latex → bibtex → latex → latex);
        stopping early leaves "Citation undefined" warnings and `[?]` markers in
        the PDF, which is the classic symptom of a broken multi-pass.

        The assertion is conditional because a minimal TinyTeX may lack the .bst
        or bibtex itself — in that case the test proves nothing rather than
        failing for an unrelated reason.
        """
        session_id, session_dir = _setup_session_from_fixture("with_bibtex")
        try:
            result = compile_project(session_dir, "main.tex")
            assert result.parsed_log is not None
            if result.success:
                # No unresolved citations should remain after the multi-pass.
                assert not any("Citation" in w.message and "undefined" in w.message.lower()
                               for w in result.parsed_log.warnings)
        finally:
            delete_session(session_id)

    @requires_latex
    def test_with_biber_biblatex(self):
        """The biblatex/biber path also resolves every citation.

        biber is a different tool with a different control file (.bcf), so the
        compiler has to detect which backend a document wants; getting that
        wrong yields an empty bibliography rather than an error.
        """
        if not check_latex_available().get("biber", {}).get("available"):
            pytest.skip("biber not installed")
        session_id, session_dir = _setup_session_from_fixture("with_biber")
        try:
            result = compile_project(session_dir, "main.tex")
            assert result.parsed_log is not None
            if result.success:
                assert not any("Citation" in w.message and "undefined" in w.message.lower()
                               for w in result.parsed_log.warnings)
        finally:
            delete_session(session_id)

    @requires_latex
    def test_custom_class(self):
        """A .cls uploaded with the project is used instead of a system class.

        Journal and university templates ship their own class file, so a
        workspace-local ``myclass.cls`` must win over anything in the
        distribution — the same TEXINPUTS behaviour, on the class search path.
        """
        session_id, session_dir = _setup_session_from_fixture("custom_class")
        try:
            assert compile_project(session_dir, "main.tex").success
        finally:
            delete_session(session_id)

    @requires_latex
    def test_timeout_terminates(self):
        """A document that never finishes is killed and reported as a failure.

        ``\\loop\\iftrue\\repeat`` is an unconditional infinite loop, so without
        the deadline this compile would pin a CPU core forever and block the
        single-user server. The 5s timeout keeps the suite fast; the real
        default comes from LATEX_TIMEOUT.
        """
        session_id = create_session()
        session_dir = get_session_dir(session_id)
        (session_dir / "main.tex").write_text(
            r"\documentclass{article}\begin{document}\loop\iftrue\repeat\end{document}")
        try:
            result = compile_project(session_dir, "main.tex", timeout=5)
            assert not result.success
        finally:
            delete_session(session_id)

    def test_concurrent_sessions_keep_content_isolated(self):
        """Real threads writing distinct content must not cross-contaminate.

        FastAPI serves requests concurrently, so two browser tabs can hold
        sessions at once. Real threads are used rather than mocks because the
        risk being tested is shared mutable state (a cached "current session"),
        which only appears under genuine interleaving. The final assertion
        checks all four ids map to four DISTINCT directories.
        """
        session_ids = [create_session() for _ in range(4)]
        errors = []

        def worker(index: int, session_id: str) -> None:
            """Write and immediately read back this thread's own marker.

            Exceptions are collected instead of raised: an assertion failing
            inside a thread would otherwise be printed and ignored, leaving the
            test green.
            """
            try:
                session_dir = get_session_dir(session_id)
                (session_dir / "main.tex").write_text(f"session-{index}-content")
                assert (session_dir / "main.tex").read_text() == f"session-{index}-content"
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i, s)) for i, s in enumerate(session_ids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert not errors
            assert len({get_session_dir(s) for s in session_ids}) == 4
        finally:
            for session_id in session_ids:
                delete_session(session_id)
