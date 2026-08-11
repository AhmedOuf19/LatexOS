"""
test_cli.py – tests for the headless command-line interface.

The CLI's exit codes are a published contract (agents and CI branch on them),
so most of these tests assert the code rather than the printed text. The
argument-parsing and target-resolution tests need no LaTeX; only the tests that
actually produce a PDF are marked ``requires_latex``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.cli import (
    EXIT_COMPILE_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    _resolve_target,
    build_parser,
    main,
)

GOOD_DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Hello.\n"
    "\\end{document}\n"
)

BROKEN_DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\undefinedcommand\n"
    "Text.\n"
    "\\end{document}\n"
)


class TestTargetResolution:
    """_resolve_target turns user input into (workspace, main_tex)."""

    def test_tex_file(self, tmp_path):
        f = tmp_path / "paper.tex"
        f.write_text(GOOD_DOC)
        workspace, main_tex = _resolve_target(f)
        assert workspace == tmp_path.resolve()
        assert main_tex == "paper.tex"

    def test_directory_detects_main(self, tmp_path):
        (tmp_path / "main.tex").write_text(GOOD_DOC)
        (tmp_path / "notes.tex").write_text("nope")
        workspace, main_tex = _resolve_target(tmp_path)
        assert main_tex == "main.tex"

    def test_non_tex_file_rejected(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hi")
        with pytest.raises(ValueError):
            _resolve_target(f)

    def test_missing_path_rejected(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _resolve_target(tmp_path / "nope.tex")

    def test_directory_without_tex_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _resolve_target(tmp_path)


class TestParser:
    def test_compile_defaults(self):
        args = build_parser().parse_args(["compile", "x.tex"])
        assert args.command == "compile"
        assert args.strict is False and args.json is False
        assert args.shell_escape is False, "shell-escape must be opt-in"

    def test_engine_choices_enforced(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["compile", "x.tex", "--engine", "notanengine"])


class TestExitCodes:
    def test_missing_file_is_usage_error(self, tmp_path, capsys):
        assert main(["compile", str(tmp_path / "nope.tex")]) == EXIT_USAGE

    @pytest.mark.requires_latex
    def test_success(self, tmp_path):
        f = tmp_path / "main.tex"
        f.write_text(GOOD_DOC)
        assert main(["compile", str(f)]) == EXIT_OK
        assert (tmp_path / "main.pdf").exists()

    @pytest.mark.requires_latex
    def test_output_flag_copies_pdf(self, tmp_path):
        f = tmp_path / "main.tex"
        f.write_text(GOOD_DOC)
        out = tmp_path / "sub" / "renamed.pdf"
        assert main(["compile", str(f), "-o", str(out)]) == EXIT_OK
        assert out.exists() and out.stat().st_size > 0

    @pytest.mark.requires_latex
    def test_recoverable_error_still_succeeds_but_strict_fails(self, tmp_path):
        """LaTeX emits a PDF despite an undefined command. Default = success
        (Overleaf behaviour); --strict = failure."""
        f = tmp_path / "main.tex"
        f.write_text(BROKEN_DOC)
        assert main(["compile", str(f)]) == EXIT_OK
        assert main(["compile", str(f), "--strict"]) == EXIT_COMPILE_FAILED

    @pytest.mark.requires_latex
    def test_json_output_is_parseable(self, tmp_path, capsys):
        f = tmp_path / "main.tex"
        f.write_text(GOOD_DOC)
        main(["compile", str(f), "--json"])
        payload = json.loads(capsys.readouterr().out)
        # The keys an automated caller depends on.
        for key in ("success", "pdf", "engine", "errors", "warnings", "log_path"):
            assert key in payload
        assert payload["success"] is True
        assert Path(payload["pdf"]).exists()


class TestCheck:
    def test_check_json_shape(self, capsys):
        from backend.cli import main as cli_main

        cli_main(["check", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert "latex_available" in payload and "tools" in payload
        # The CLI must never silently default to allowing shell escape.
        assert payload["shell_escape_default"] is False
