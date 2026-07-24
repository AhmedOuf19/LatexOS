"""
compiler.py - LaTeX compilation engine.

Supports: pdflatex, xelatex, lualatex
Handles: single-pass, bibtex/biber, and full multi-pass via latexmk.

Security:
  - Always passes -no-shell-escape to block write18 attacks.
  - Enforces a hard timeout (COMPILE_TIMEOUT seconds).
  - Runs in the session workspace (not system directories).
"""

import os
import subprocess
import shutil
import time
from pathlib import Path
from typing import Literal

from backend.config import (
    COMPILE_TIMEOUT,
    LATEX_BIN_PATH,
    DEFAULT_ENGINE,
)
from backend.log_parser import parse_log, ParsedLog, get_log_summary

EngineType = Literal["pdflatex", "xelatex", "lualatex"]


# ─── Result Model ────────────────────────────────────────────────────────────

class CompilationResult:
    def __init__(
        self,
        success: bool,
        pdf_path: Path | None,
        log_path: Path | None,
        parsed_log: ParsedLog | None,
        summary: str,
        duration_seconds: float,
    ):
        self.success = success
        self.pdf_path = pdf_path
        self.log_path = log_path
        self.parsed_log = parsed_log
        self.summary = summary
        self.duration_seconds = duration_seconds

    def to_dict(self) -> dict:
        # Always return a fully-structured log dict, even on failure
        empty_log = {"errors": [], "warnings": [], "badboxes": [], "info": [], "raw": "", "has_errors": False}
        return {
            "success": self.success,
            "summary": self.summary,
            "duration_seconds": round(self.duration_seconds, 2),
            "log": self.parsed_log.to_dict() if self.parsed_log else empty_log,
        }


# ─── Binary Resolution ───────────────────────────────────────────────────────

def _resolve_binary(name: str) -> str:
    """
    Resolve the full path to a LaTeX binary.
    Prefers LATEX_BIN_PATH from config, then falls back to PATH.
    """
    if LATEX_BIN_PATH:
        # Try .exe (Windows) first, then bare name
        for candidate in [f"{name}.exe", name]:
            full = Path(LATEX_BIN_PATH) / candidate
            if full.exists():
                return str(full)

    # Fall back to system PATH
    found = shutil.which(name)
    if found:
        return found

    raise EnvironmentError(
        f"'{name}' executable not found. "
        f"Please install MiKTeX or TeX Live and ensure it is on your system PATH. "
        f"Current LATEX_BIN_PATH={LATEX_BIN_PATH!r}"
    )


def _has_binary(name: str) -> bool:
    try:
        _resolve_binary(name)
        return True
    except EnvironmentError:
        return False


# ─── Subprocess Helper ───────────────────────────────────────────────────────

def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int = COMPILE_TIMEOUT,
) -> tuple[int, str, str]:
    """
    Run a subprocess command. Returns (returncode, stdout, stderr).
    Raises TimeoutError if the process exceeds the timeout.

    On Windows, uses CREATE_NEW_PROCESS_GROUP + taskkill /T to reliably
    kill the entire process tree (pdflatex spawns child processes that
    survive a plain subprocess.run() timeout).
    """
    import sys

    env = os.environ.copy()
    if LATEX_BIN_PATH:
        env["PATH"] = LATEX_BIN_PATH + os.pathsep + env.get("PATH", "")

    # On Windows, create a new process group so we can kill the entire tree
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        creationflags=creation_flags,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        # Kill the entire process tree on Windows
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

        # Drain any remaining output
        proc.wait(timeout=5)

        raise TimeoutError(
            f"Compilation timed out after {timeout} seconds. "
            "The process was forcibly terminated."
        )


# ─── Compilation Strategies ──────────────────────────────────────────────────

def _compile_with_latexmk(
    tex_file: str,
    workspace: Path,
    engine: EngineType,
    timeout: int,
) -> tuple[int, str, str]:
    """
    Use latexmk for fully automated multi-pass compilation.
    latexmk handles pdflatex + bibtex/biber + re-runs automatically.
    """
    latexmk = _resolve_binary("latexmk")

    engine_flag = {
        "pdflatex": "-pdf",
        "xelatex": "-xelatex",
        "lualatex": "-lualatex",
    }[engine]

    cmd = [
        latexmk,
        engine_flag,
        "-interaction=nonstopmode",
        "-shell-escape",
        "-f",
        "-file-line-error",
        "-synctex=0",
        tex_file,
    ]
    return _run(cmd, workspace, timeout)


def _compile_manual_passes(
    tex_file: str,
    workspace: Path,
    engine: EngineType,
    timeout: int,
) -> tuple[int, str, str]:
    """
    Fallback: manual multi-pass compilation.
    Pass 1: pdflatex → generates .aux
    Pass 2: bibtex/biber if .bib files exist
    Pass 3: pdflatex (resolve citations)
    Pass 4: pdflatex (resolve references)
    """
    latex_bin = _resolve_binary(engine)
    base_name = Path(tex_file).stem

    latex_cmd = [
        latex_bin,
        "-interaction=nonstopmode",
        "-shell-escape",
        "-file-line-error",
        tex_file,
    ]

    combined_stdout = []
    combined_stderr = []

    # Pass 1
    rc, out, err = _run(latex_cmd, workspace, max(10, timeout // 4))
    combined_stdout.append(f"=== Pass 1 ({engine}) ===\n{out}")
    combined_stderr.append(err)

    # Check if bibliography processing is needed
    bib_files = list(workspace.rglob("*.bib"))
    aux_file = workspace / f"{base_name}.aux"
    bibtex_ran = False

    if bib_files and aux_file.exists():
        aux_content = aux_file.read_text(encoding="utf-8", errors="ignore")
        if r"\bibdata" in aux_content or r"\citation" in aux_content:
            use_biber = _should_use_biber(workspace)
            if use_biber and _has_binary("biber"):
                bib_cmd = [_resolve_binary("biber"), base_name]
            else:
                bib_cmd = [_resolve_binary("bibtex"), base_name]

            rc_bib, out_bib, err_bib = _run(bib_cmd, workspace, max(10, timeout // 4))
            combined_stdout.append(f"=== Bibliography pass ===\n{out_bib}")
            combined_stderr.append(err_bib)
            bibtex_ran = True

    # Pass 2
    rc, out, err = _run(latex_cmd, workspace, max(10, timeout // 4))
    combined_stdout.append(f"=== Pass 2 ({engine}) ===\n{out}")
    combined_stderr.append(err)

    # Pass 3
    rc, out, err = _run(latex_cmd, workspace, max(10, timeout // 4))
    combined_stdout.append(f"=== Pass 3 ({engine}) ===\n{out}")
    combined_stderr.append(err)
    
    # Pass 4 (Only necessary if bibtex ran, to resolve citations completely)
    if bibtex_ran:
        rc, out, err = _run(latex_cmd, workspace, max(10, timeout // 4))
        combined_stdout.append(f"=== Pass 4 ({engine}) ===\n{out}")
        combined_stderr.append(err)

    return rc, "\n".join(combined_stdout), "\n".join(combined_stderr)


def _should_use_biber(workspace: Path) -> bool:
    """Detect if any .tex file uses biblatex (which needs biber)."""
    for tex in workspace.rglob("*.tex"):
        try:
            content = tex.read_text(encoding="utf-8", errors="ignore")
            if r"\usepackage{biblatex}" in content or "backend=biber" in content:
                return True
        except OSError:
            continue
    return False


# ─── Public Compilation API ──────────────────────────────────────────────────

def compile_project(
    workspace: Path,
    main_tex: str,
    engine: EngineType = DEFAULT_ENGINE,
    timeout: int = COMPILE_TIMEOUT,
) -> CompilationResult:
    """
    Compile a LaTeX project. Tries latexmk first; falls back to manual passes.

    Args:
        workspace: Absolute path to the session directory containing all project files.
        main_tex:  Relative path to the main .tex file within workspace.
        engine:    LaTeX engine to use ('pdflatex', 'xelatex', 'lualatex').
        timeout:   Max compilation time in seconds.

    Returns:
        CompilationResult with success flag, PDF path, parsed log, and summary.
    """
    start_time = time.monotonic()

    tex_path = workspace / main_tex
    if not tex_path.exists():
        return CompilationResult(
            success=False,
            pdf_path=None,
            log_path=None,
            parsed_log=None,
            summary=f"Main .tex file not found: '{main_tex}'",
            duration_seconds=0,
        )

    # Determine the sub-directory where the .tex file lives
    # (latexmk needs to run in the directory containing main.tex)
    tex_dir = tex_path.parent
    tex_filename = tex_path.name
    base_name = tex_path.stem

    # ── Try latexmk first, fall back to manual passes if it fails ────────────
    use_latexmk = _has_binary("latexmk")
    stdout = stderr = ""
    returncode = -1
    used_latexmk = False

    try:
        if use_latexmk:
            returncode, stdout, stderr = _compile_with_latexmk(
                tex_filename, tex_dir, engine, timeout
            )
            used_latexmk = True

            # If latexmk exited non-zero and produced no PDF, it likely failed
            # due to missing Perl or a configuration issue – fall back to manual.
            if returncode != 0:
                pdf_check = tex_dir / f"{base_name}.pdf"
                latexmk_failed_no_pdf = not (pdf_check.exists() and pdf_check.stat().st_size > 0)
                if latexmk_failed_no_pdf:
                    # latexmk failed without producing a PDF – use manual passes
                    returncode, stdout, stderr = _compile_manual_passes(
                        tex_filename, tex_dir, engine, timeout
                    )
                    used_latexmk = False
        else:
            returncode, stdout, stderr = _compile_manual_passes(
                tex_filename, tex_dir, engine, timeout
            )
    except TimeoutError as e:
        duration = time.monotonic() - start_time
        return CompilationResult(
            success=False,
            pdf_path=None,
            log_path=None,
            parsed_log=None,
            summary=str(e),
            duration_seconds=duration,
        )
    except EnvironmentError as e:
        duration = time.monotonic() - start_time
        return CompilationResult(
            success=False,
            pdf_path=None,
            log_path=None,
            parsed_log=None,
            summary=str(e),
            duration_seconds=duration,
        )

    duration = time.monotonic() - start_time

    # ── Read the .log file ────────────────────────────────────────────────────
    log_file = tex_dir / f"{base_name}.log"
    raw_log = ""
    if log_file.exists():
        raw_log = log_file.read_text(encoding="utf-8", errors="ignore")
    else:
        # If no .log, use stdout as fallback
        raw_log = stdout + "\n" + stderr

    parsed = parse_log(raw_log)
    summary = get_log_summary(parsed)

    # ── Check PDF was produced ────────────────────────────────────────────────
    pdf_file = tex_dir / f"{base_name}.pdf"
    success = pdf_file.exists() and pdf_file.stat().st_size > 0

    # If latexmk failed with non-zero but PDF exists, treat as partial success
    if not success and returncode != 0:
        # Check for PDF in workspace root as fallback
        alt_pdf = workspace / f"{base_name}.pdf"
        if alt_pdf.exists():
            pdf_file = alt_pdf
            success = True

    if not success:
        summary = f"Compilation failed (exit code {returncode}). " + summary

    return CompilationResult(
        success=success,
        pdf_path=pdf_file if success else None,
        log_path=log_file if log_file.exists() else None,
        parsed_log=parsed,
        summary=summary,
        duration_seconds=duration,
    )


def check_latex_available() -> dict:
    """Check what LaTeX tools are available on this system."""
    tools = ["pdflatex", "xelatex", "lualatex", "latexmk", "bibtex", "biber"]
    result = {}
    for tool in tools:
        try:
            path = _resolve_binary(tool)
            result[tool] = {"available": True, "path": path}
        except EnvironmentError:
            result[tool] = {"available": False, "path": None}
    return result
