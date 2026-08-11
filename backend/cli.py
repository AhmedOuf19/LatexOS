"""
cli.py – Headless command-line interface for LaTeX Studio.

The web UI is the product; this module is the same compile engine with a
terminal front end, for the cases where a browser is the wrong shape: CI jobs,
Makefiles, and AI coding agents (Claude Code) that need to turn a ``.tex`` file
into a PDF and read back *why* it failed.

    python -m backend.cli compile paper.tex
    python -m backend.cli compile paper.tex -o out/paper.pdf --engine xelatex
    python -m backend.cli compile thesis/ --json
    python -m backend.cli check

Design decisions a reader would otherwise have to reverse-engineer
------------------------------------------------------------------
* **No server, no session.** ``compile_project()`` only needs a directory and a
  main file, so the CLI calls it directly. Nothing binds a port, so this is safe
  to run in CI and cannot collide with a running UI.
* **Compiles in place, next to the source.** That is what every other LaTeX tool
  does, and it keeps relative ``\\input``/``\\includegraphics`` paths working
  without copying the project anywhere. ``--output`` copies the finished PDF
  elsewhere afterwards rather than changing where the compile happens.
* **Two output modes, one source of truth.** Human mode prints a short report;
  ``--json`` prints a machine-readable object. Both are rendered from the same
  ``CompilationResult``, so they can never disagree.
* **Exit codes are the contract** (documented in ``EXIT_*`` below). A caller
  should branch on the exit code and read stdout only for detail — the wording
  of the human report is not a stable interface.
* **Errors are truncated, not dropped.** A broken document can emit hundreds of
  cascading errors; the first few identify the cause and the rest are noise, so
  the report shows ``MAX_REPORTED`` and says how many were hidden.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from backend import __version__

# Exit codes. These are the stable, scriptable part of this interface.
EXIT_OK = 0            # a PDF was produced (it may still have warnings)
EXIT_COMPILE_FAILED = 1  # LaTeX ran but produced no PDF
EXIT_USAGE = 2         # bad arguments / input file missing
EXIT_ENVIRONMENT = 3   # no LaTeX distribution available

# A broken preamble cascades; the first handful of errors carry the signal.
MAX_REPORTED = 8


def _resolve_target(target: Path) -> tuple[Path, str]:
    """Turn a user-supplied path into the ``(workspace, main_tex)`` pair that
    ``compile_project`` expects.

    Accepts either a ``.tex`` file (the common case — compile exactly this file)
    or a directory (find the main file the same way the web UI does, so both
    front ends pick the same entry point for the same project).
    """
    from backend.file_manager import detect_main_tex

    target = target.expanduser().resolve()

    if target.is_file():
        if target.suffix.lower() != ".tex":
            raise ValueError(f"Not a .tex file: {target}")
        return target.parent, target.name

    if target.is_dir():
        main = detect_main_tex(target)
        if not main:
            raise ValueError(f"No .tex file found in: {target}")
        return target, main

    raise FileNotFoundError(f"No such file or directory: {target}")


def _result_to_dict(result, workspace: Path, main_tex: str, pdf: Path | None) -> dict:
    """Flatten a ``CompilationResult`` into the ``--json`` payload.

    Deliberately a *summary*, not a dump: the raw log can be megabytes, so the
    caller gets the parsed errors/warnings plus ``log_path`` to read the rest if
    it actually needs to.
    """
    log = result.parsed_log

    def entries(items):
        return [
            {
                "message": e.message,
                "file": e.file or None,
                "line": e.line,
            }
            for e in items
        ]

    return {
        "success": result.success,
        "pdf": str(pdf) if pdf else None,
        "workspace": str(workspace),
        "main_tex": main_tex,
        "engine": result.engine,
        "summary": result.summary,
        "duration_seconds": round(result.duration_seconds, 2),
        "returncode": result.returncode,
        "errors": entries(log.errors) if log else [],
        "warnings": entries(log.warnings) if log else [],
        # Over/underfull boxes are cosmetic (the PDF is valid either way), but a
        # caller diagnosing layout problems needs them - and they carry the line
        # numbers that say WHERE the text overflows. Excluded from the human
        # report, which would otherwise be hundreds of lines long.
        "badboxes": entries(log.badboxes) if log else [],
        "log_path": str(result.log_path) if result.log_path else None,
    }


def _print_human(payload: dict) -> None:
    """Print the short report. Detail is proportional to how badly it went:
    a clean build is two lines, a failure leads with the errors."""
    if payload["success"]:
        print(f"[OK] PDF: {payload['pdf']}")
        print(f"     {payload['summary']} ({payload['duration_seconds']}s, {payload['engine']})")
    else:
        print(f"[FAILED] {payload['summary']}")
        print(f"         engine={payload['engine']}  exit={payload['returncode']}")

    errors = payload["errors"]
    for e in errors[:MAX_REPORTED]:
        location = ""
        if e["file"]:
            location = f"{e['file']}"
            if e["line"]:
                location += f":{e['line']}"
            location = f" [{location}]"
        print(f"  error:{location} {e['message']}")
    if len(errors) > MAX_REPORTED:
        print(f"  ... and {len(errors) - MAX_REPORTED} more error(s)")

    # Warnings are listed only when the build failed, where they often explain
    # the failure. On a successful build they are noise the summary covers.
    if not payload["success"] and payload["warnings"]:
        for w in payload["warnings"][:3]:
            print(f"  warning: {w['message']}")

    if not payload["success"] and payload["log_path"]:
        print(f"  full log: {payload['log_path']}")


def cmd_compile(args: argparse.Namespace) -> int:
    """Compile one document and report the outcome."""
    from backend.compiler import compile_project

    try:
        workspace, main_tex = _resolve_target(Path(args.target))
    except (ValueError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_USAGE

    result = compile_project(
        workspace=workspace,
        main_tex=main_tex,
        engine=args.engine,
        timeout=args.timeout,
        allow_shell_escape=True if args.shell_escape else None,
    )

    # Copy the PDF to --output if asked. Done after the compile so the build
    # itself stays in the project directory where relative paths resolve.
    pdf = result.pdf_path
    if result.success and args.output:
        destination = Path(args.output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(result.pdf_path, destination)
        pdf = destination

    payload = _result_to_dict(result, workspace, main_tex, pdf)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_human(payload)

    if not result.success:
        return EXIT_COMPILE_FAILED
    # --strict turns "a PDF, but the log has errors" into a failure. Without it
    # that case is a success, matching how Overleaf and latexmk behave.
    if args.strict and payload["errors"]:
        return EXIT_COMPILE_FAILED
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """Report which LaTeX tools are available.

    Exists so a caller can distinguish "this document is broken" from "there is
    no LaTeX on this machine" *before* compiling, and so a human debugging a
    failed compile can see the toolchain in one command.
    """
    from backend.compiler import check_latex_available
    from backend.config import ALLOW_SHELL_ESCAPE

    tools = check_latex_available()
    has_pdflatex = tools.get("pdflatex", {}).get("available", False)

    if args.json:
        print(json.dumps({
            "version": __version__,
            "latex_available": has_pdflatex,
            "shell_escape_default": ALLOW_SHELL_ESCAPE,
            "tools": {name: info["path"] for name, info in tools.items() if info["available"]},
        }, indent=2))
    else:
        print(f"LaTeX Studio {__version__}")
        for name, info in tools.items():
            mark = "OK  " if info["available"] else "--  "
            print(f"  [{mark}] {name}{': ' + info['path'] if info['available'] else ''}")
        if not has_pdflatex:
            print("\n  pdflatex not found - run install.bat, or install TinyTeX/MiKTeX/TeX Live.")

    return EXIT_OK if has_pdflatex else EXIT_ENVIRONMENT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latex-studio",
        description="Compile LaTeX to PDF from the command line (no browser needed).",
    )
    parser.add_argument("--version", action="version", version=f"LaTeX Studio {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser(
        "compile", help="compile a .tex file or a project directory to PDF"
    )
    compile_parser.add_argument(
        "target", help="path to a .tex file, or a directory containing the project"
    )
    compile_parser.add_argument(
        "-o", "--output", help="copy the finished PDF here (default: leave it beside the source)"
    )
    compile_parser.add_argument(
        "--engine", default=None,
        choices=["pdflatex", "xelatex", "lualatex"],
        help="LaTeX engine (default: the configured one, normally pdflatex)",
    )
    compile_parser.add_argument(
        "--timeout", type=int, default=None, help="seconds before the compile is abandoned"
    )
    compile_parser.add_argument(
        "--shell-escape", action="store_true",
        help="allow \\write18 (needed by minted). Only for documents you trust - "
             "it lets the document run programs on this machine.",
    )
    compile_parser.add_argument(
        "--strict", action="store_true",
        help="fail (exit 1) if the log contains any error, even when a PDF was "
             "still produced. LaTeX recovers from many errors and emits a PDF "
             "anyway; use this when you want 'clean or nothing'.",
    )
    compile_parser.add_argument(
        "--json", action="store_true", help="print a machine-readable result object"
    )
    compile_parser.set_defaults(func=cmd_compile)

    check_parser = subparsers.add_parser("check", help="report the available LaTeX tools")
    check_parser.add_argument("--json", action="store_true", help="machine-readable output")
    check_parser.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Fill in config-derived defaults here rather than at parser-build time, so
    # importing this module never triggers the filesystem scan in config.py.
    if getattr(args, "engine", None) is None and args.command == "compile":
        from backend.config import DEFAULT_ENGINE
        args.engine = DEFAULT_ENGINE
    if getattr(args, "timeout", None) is None and args.command == "compile":
        from backend.config import COMPILE_TIMEOUT
        args.timeout = COMPILE_TIMEOUT

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[ABORTED]", file=sys.stderr)
        return EXIT_USAGE
    except EnvironmentError as exc:
        # Raised by the compiler when a required binary is missing - a different
        # problem from a document that failed to compile, hence its own code.
        print(f"[ENVIRONMENT] {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT


if __name__ == "__main__":
    sys.exit(main())
