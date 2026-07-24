"""
compiler.py – LaTeX compilation engine.

Supports pdflatex, xelatex and lualatex, and two compilation strategies:

* **latexmk** (preferred) – runs as many passes as needed and picks bibtex or
  biber automatically. Used whenever latexmk (and Perl) are available.
* **manual multi-pass** (fallback) – used only when latexmk itself cannot run.
  Runs the engine, decides between bibtex and biber from the .aux/.bcf files,
  and then re-runs the engine until cross-references stabilise.

Security posture (see backend/config.py for the switches)
---------------------------------------------------------
* Shell-escape is DISABLED by default (``-no-shell-escape``). \\write18 cannot
  run OS commands unless the user explicitly opts in with
  ``LATEX_ALLOW_SHELL_ESCAPE=1``.
* ``openin_any`` / ``openout_any`` are set to ``p`` (paranoid) so a document
  cannot \\input or write files outside its own workspace.
* Compilation runs inside the session workspace and is bounded by a single
  wall-clock deadline (``COMPILE_TIMEOUT``) shared across all passes.

Correctness notes
------------------
* Build artifacts are DELETED before every compile, so "a non-empty PDF exists"
  is a correct freshness test – a stale PDF from a previous run can never make a
  failed compile look successful.
* ``TEXINPUTS`` / ``BIBINPUTS`` / ``BSTINPUTS`` include the workspace
  recursively, so resources uploaded into sub-folders are found.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from backend.config import (
    ALLOW_SHELL_ESCAPE,
    AUTO_INSTALL_PACKAGES,
    COMPILE_TIMEOUT,
    DEFAULT_ENGINE,
    EngineType,
    LATEX_BIN_PATH,
    MAX_LOG_READ_BYTES,
)
from backend.log_parser import ParsedLog, get_log_summary, parse_log

# Build artifact extensions removed before each compile so success can be judged
# by the presence of a *freshly produced* PDF rather than a leftover file.
_ARTIFACT_EXTENSIONS = (
    ".pdf", ".aux", ".log", ".out", ".toc", ".lof", ".lot",
    ".bcf", ".bbl", ".blg", ".run.xml", ".fls", ".fdb_latexmk",
    ".synctex.gz", ".idx", ".ind", ".ilg", ".glo", ".gls", ".glg",
    ".nav", ".snm", ".vrb", ".nlo", ".nls",
)

# Log markers that mean "run the engine again to settle references/TOC".
_RE_RERUN = re.compile(
    r"Rerun to get|Rerun LaTeX|Label\(s\) may have changed|"
    r"Please \(re\)run|run LaTeX again",
    re.IGNORECASE,
)

# Missing-file markers used to drive on-demand package installation.
_RE_MISSING_FILE = re.compile(
    r"(?:File `([^']+?\.(?:sty|cls|tex))' not found"
    r"|Font \\[^=]*=([\w-]+)|"
    r"Cannot find file `([^']+)')",
)

# Maximum engine passes in the manual path (guards against an unstable document
# looping forever) and maximum on-demand install/retry cycles.
_MAX_MANUAL_PASSES = 5
_MAX_INSTALL_RETRIES = 3


# ─── Result Model ────────────────────────────────────────────────────────────

class CompilationResult:
    """Outcome of a compile, returned to the API layer.

    ``success`` means "a fresh, non-empty PDF was produced". ``returncode`` and
    ``parsed_log.has_errors`` are exposed separately so the UI can show
    "compiled with errors" while still rendering a PDF (Overleaf-style),
    without conflating "the document had errors" with "no output at all".
    """

    def __init__(
        self,
        success: bool,
        pdf_path: Path | None,
        log_path: Path | None,
        parsed_log: ParsedLog | None,
        summary: str,
        duration_seconds: float,
        returncode: int | None = None,
        engine: str = DEFAULT_ENGINE,
    ):
        self.success = success
        self.pdf_path = pdf_path
        self.log_path = log_path
        self.parsed_log = parsed_log
        self.summary = summary
        self.duration_seconds = duration_seconds
        self.returncode = returncode
        self.engine = engine

    def to_dict(self) -> dict:
        # Always return a fully-structured log dict, even on failure, so the
        # frontend can rely on the keys existing.
        empty_log = {
            "errors": [], "warnings": [], "badboxes": [], "info": [],
            "raw": "", "has_errors": False,
        }
        return {
            "success": self.success,
            "summary": self.summary,
            "duration_seconds": round(self.duration_seconds, 2),
            "returncode": self.returncode,
            "engine": self.engine,
            "log": self.parsed_log.to_dict() if self.parsed_log else empty_log,
        }


# ─── A shared wall-clock deadline ────────────────────────────────────────────

class _Deadline:
    """Tracks a single budget shared by every subprocess in one compile.

    Each pass asks for ``remaining()`` seconds, so the *total* time across all
    passes (and the latexmk attempt plus any manual fallback) can never exceed
    ``COMPILE_TIMEOUT`` – which is what "hard timeout" is supposed to mean.
    """

    def __init__(self, total_seconds: int):
        self._end = time.monotonic() + total_seconds

    def remaining(self) -> int:
        # Never return < 1s; a 0s timeout would kill a process instantly.
        return max(1, int(self._end - time.monotonic()))

    def expired(self) -> bool:
        return time.monotonic() >= self._end


# ─── Binary Resolution ───────────────────────────────────────────────────────

def _resolve_binary(name: str) -> str:
    """Return the full path to a LaTeX binary.

    Prefers ``LATEX_BIN_PATH`` from config (the folder-local or detected distro)
    and falls back to PATH. Raises ``EnvironmentError`` if the tool is missing.
    """
    if LATEX_BIN_PATH:
        for candidate in (f"{name}.exe", name):
            full = Path(LATEX_BIN_PATH) / candidate
            if full.exists():
                return str(full)

    found = shutil.which(name)
    if found:
        return found

    raise EnvironmentError(
        f"'{name}' executable not found. Install a LaTeX distribution "
        f"(TinyTeX / MiKTeX / TeX Live) or set LATEX_BIN_PATH. "
        f"Current LATEX_BIN_PATH={LATEX_BIN_PATH!r}"
    )


def _has_binary(name: str) -> bool:
    try:
        _resolve_binary(name)
        return True
    except EnvironmentError:
        return False


# ─── Environment for the subprocess ──────────────────────────────────────────

def _build_env(workspace: Path) -> dict:
    """Construct the environment for LaTeX subprocesses.

    * Puts the LaTeX bin dir first on PATH.
    * ``openin_any`` / ``openout_any`` = ``p`` confine file reads/writes to the
      current tree (blocks ``\\input{C:/…/secret}`` exfiltration).
    * ``TEXINPUTS`` / ``BIBINPUTS`` / ``BSTINPUTS`` include the workspace
      recursively (the ``//`` suffix) so resources in sub-folders resolve.
    * ``OSFONTDIR`` lets xelatex/lualatex find uploaded .ttf/.otf fonts.
    """
    env = os.environ.copy()
    if LATEX_BIN_PATH:
        env["PATH"] = LATEX_BIN_PATH + os.pathsep + env.get("PATH", "")

    # Confine file I/O to the workspace subtree.
    env["openin_any"] = "p"
    env["openout_any"] = "p"
    env["TEXMFOUTPUT"] = str(workspace)

    # kpathsea uses '//' to mean "search this directory recursively". A trailing
    # os.pathsep keeps the distribution's own defaults on the search path.
    ws = str(workspace).replace(os.sep, "/")
    recursive = f"{ws}//{os.pathsep}"
    for var in ("TEXINPUTS", "BIBINPUTS", "BSTINPUTS"):
        env[var] = recursive + env.get(var, "")
    env["OSFONTDIR"] = str(workspace)

    return env


# ─── Subprocess Helper ───────────────────────────────────────────────────────

def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    env: dict,
) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr).

    Raises ``TimeoutError`` if the process exceeds ``timeout``. On both Windows
    and POSIX the *entire process tree* is killed on timeout, because pdflatex
    can spawn helper processes that outlive a plain terminate.
    """
    creation_flags = 0
    start_new_session = False
    if sys.platform == "win32":
        # New process group so taskkill /T can reach the whole tree.
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # Put the child in its own session/process group, so os.killpg targets
        # only the compile – NOT the uvicorn server that launched it.
        start_new_session = True

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creation_flags,
        start_new_session=start_new_session,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise TimeoutError(
            f"Compilation timed out after {timeout} seconds and was terminated."
        )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Forcibly kill a process and its children on either platform."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


# ─── Text / artifact helpers ─────────────────────────────────────────────────

def _read_text_lossless(path: Path, max_bytes: int = MAX_LOG_READ_BYTES) -> str:
    """Read a (possibly huge, possibly non-UTF-8) log/aux file without losing
    bytes.

    LaTeX logs mix UTF-8 with the OS ANSI codepage (accented file names, etc.).
    We try UTF-8 first and fall back to latin-1, which round-trips every byte so
    no character is silently dropped. Reads at most ``max_bytes``.
    """
    try:
        data = path.read_bytes()[:max_bytes]
    except OSError:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _clean_artifacts(tex_dir: Path, base_name: str, workspace: Path) -> None:
    """Delete build artifacts for ``base_name`` before compiling.

    Removes them from both the .tex's own directory and the workspace root
    (some engines drop the PDF at the root), so no stale output survives.
    """
    for ext in _ARTIFACT_EXTENSIONS:
        for folder in {tex_dir, workspace}:
            artifact = folder / f"{base_name}{ext}"
            try:
                artifact.unlink(missing_ok=True)
            except OSError:
                pass  # a locked/again-in-use file is not worth failing over


def _aux_snapshot(tex_dir: Path, base_name: str) -> str:
    """Return the .aux + .toc content, used to detect when reruns have settled."""
    snap = ""
    for ext in (".aux", ".toc"):
        f = tex_dir / f"{base_name}{ext}"
        if f.exists():
            snap += _read_text_lossless(f)
    return snap


# ─── Bibliography detection ──────────────────────────────────────────────────

# Matches \usepackage{biblatex} with or without options, e.g.
# \usepackage[style=numeric,backend=biber]{biblatex}
_RE_BIBLATEX = re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{biblatex\}")


def _uses_biblatex(workspace: Path) -> bool:
    """True if any .tex loads biblatex (which is processed by biber)."""
    for tex in workspace.rglob("*.tex"):
        try:
            content = tex.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _RE_BIBLATEX.search(content) or "backend=biber" in content:
            return True
    return False


# ─── On-demand package installation (TinyTeX / TeX Live) ─────────────────────

def _install_missing_packages(raw_log: str, deadline: _Deadline) -> bool:
    """Try to install packages a failed compile reported as missing.

    Only runs for TinyTeX/TeX Live (via ``tlmgr``); MiKTeX installs missing
    packages on its own. Returns ``True`` if at least one package was installed
    (so the caller should recompile). Best-effort and fully guarded – any
    failure just means "installed nothing".
    """
    if not AUTO_INSTALL_PACKAGES or not _has_binary("tlmgr"):
        return False

    # Collect the missing file names (foo.sty, bar.cls, …) from the log.
    missing: set[str] = set()
    for m in _RE_MISSING_FILE.finditer(raw_log):
        name = next((g for g in m.groups() if g), None)
        if name:
            missing.add(name.strip())
    if not missing:
        return False

    tlmgr = _resolve_binary("tlmgr")
    installed_any = False
    env = os.environ.copy()
    if LATEX_BIN_PATH:
        env["PATH"] = LATEX_BIN_PATH + os.pathsep + env.get("PATH", "")

    for fname in list(missing)[:_MAX_INSTALL_RETRIES]:
        if deadline.expired():
            break
        # Resolve the file to a TeX Live package name, then install it.
        pkg = _tlmgr_package_for_file(tlmgr, fname, env, deadline)
        if not pkg:
            continue
        try:
            rc, _out, _err = _run(
                [tlmgr, "install", pkg], Path.cwd(), deadline.remaining(), env
            )
            if rc == 0:
                installed_any = True
        except (TimeoutError, OSError):
            break
    return installed_any


def _tlmgr_package_for_file(
    tlmgr: str, fname: str, env: dict, deadline: _Deadline
) -> str | None:
    """Map a missing file (foo.sty) to the tlmgr package that provides it."""
    try:
        rc, out, _err = _run(
            [tlmgr, "search", "--global", "--file", f"/{fname}"],
            Path.cwd(), min(30, deadline.remaining()), env,
        )
    except (TimeoutError, OSError):
        return None
    if rc != 0:
        # Fall back to the file stem as a best guess (often correct, e.g. tikz).
        return Path(fname).stem
    # tlmgr prints "pkgname:" lines above the matching file paths.
    for line in out.splitlines():
        line = line.strip()
        if line.endswith(":") and "/" not in line:
            return line[:-1]
    return Path(fname).stem


# ─── Compilation Strategies ──────────────────────────────────────────────────

def _latexmk_cmd(tex_file: str, engine: EngineType) -> list[str]:
    """Build the latexmk command line for the chosen engine."""
    engine_flag = {
        "pdflatex": "-pdf",
        "xelatex": "-xelatex",
        "lualatex": "-lualatex",
    }[engine]
    shell_flag = "-shell-escape" if ALLOW_SHELL_ESCAPE else "-no-shell-escape"
    return [
        "latexmk",
        engine_flag,
        "-interaction=nonstopmode",
        shell_flag,
        "-f",  # force: keep going after a document error so a PDF is still emitted
        "-file-line-error",
        "-synctex=0",
        tex_file,
    ]


def _compile_with_latexmk(
    tex_file: str, workspace: Path, engine: EngineType,
    deadline: _Deadline, env: dict,
) -> tuple[int, str, str]:
    """Run latexmk once (it handles its own multi-pass + bib logic)."""
    latexmk = _resolve_binary("latexmk")
    cmd = _latexmk_cmd(tex_file, engine)
    cmd[0] = latexmk
    return _run(cmd, workspace, deadline.remaining(), env)


def _engine_cmd(engine_bin: str, tex_file: str) -> list[str]:
    """Build a single-pass engine command with the current shell-escape policy."""
    shell_flag = "-shell-escape" if ALLOW_SHELL_ESCAPE else "-no-shell-escape"
    return [
        engine_bin,
        "-interaction=nonstopmode",
        shell_flag,
        "-file-line-error",
        tex_file,
    ]


def _compile_manual_passes(
    tex_file: str, workspace: Path, engine: EngineType,
    deadline: _Deadline, env: dict,
) -> tuple[int, str, str]:
    """Fallback multi-pass compile used when latexmk cannot run.

    Pass 1 → bibliography (bibtex or biber, auto-detected) → rerun the engine
    until the .aux/.toc stop changing (or ``_MAX_MANUAL_PASSES`` is reached).
    """
    engine_bin = _resolve_binary(engine)
    base_name = Path(tex_file).stem
    cmd = _engine_cmd(engine_bin, tex_file)

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    returncode = 0

    def run_engine(label: str) -> None:
        nonlocal returncode
        rc, out, err = _run(cmd, workspace, deadline.remaining(), env)
        returncode = rc
        out_chunks.append(f"=== {label} ({engine}) ===\n{out}")
        err_chunks.append(err)

    # Pass 1 – generates the .aux / .bcf that tell us how to run the bibliography.
    run_engine("Pass 1")

    # Bibliography pass, if the document actually asked for one.
    _run_bibliography(base_name, workspace, deadline, env, out_chunks, err_chunks)

    # Re-run until references settle, bounded by _MAX_MANUAL_PASSES total passes.
    prev_snapshot = _aux_snapshot(workspace, base_name)
    for i in range(2, _MAX_MANUAL_PASSES + 1):
        if deadline.expired():
            break
        run_engine(f"Pass {i}")
        snapshot = _aux_snapshot(workspace, base_name)
        log_text = _read_text_lossless(workspace / f"{base_name}.log")
        if snapshot == prev_snapshot and not _RE_RERUN.search(log_text):
            break  # stable – nothing more to gain
        prev_snapshot = snapshot

    return returncode, "\n".join(out_chunks), "\n".join(err_chunks)


def _run_bibliography(
    base_name: str, workspace: Path, deadline: _Deadline, env: dict,
    out_chunks: list[str], err_chunks: list[str],
) -> None:
    """Run biber or bibtex if the document uses a bibliography.

    biber is chosen when a ``.bcf`` control file exists or the .tex uses
    biblatex; otherwise the traditional bibtex path (\\bibliography +
    \\bibliographystyle, which writes ``\\bibdata``/``\\citation`` into the .aux)
    is used. A non-zero bibliography exit is surfaced in the output so a failed
    bibliography is not silently swallowed.
    """
    bcf = workspace / f"{base_name}.bcf"
    aux = workspace / f"{base_name}.aux"
    aux_text = _read_text_lossless(aux) if aux.exists() else ""

    tool: str | None = None
    if bcf.exists() or _uses_biblatex(workspace):
        if _has_binary("biber"):
            tool = "biber"
        elif r"\bibdata" in aux_text and _has_binary("bibtex"):
            tool = "bibtex"  # biblatex with backend=bibtex
    elif (r"\bibdata" in aux_text or r"\citation" in aux_text) and _has_binary("bibtex"):
        tool = "bibtex"

    if not tool:
        return

    try:
        rc, out, err = _run(
            [_resolve_binary(tool), base_name], workspace,
            deadline.remaining(), env,
        )
    except (TimeoutError, EnvironmentError) as exc:
        out_chunks.append(f"=== Bibliography ({tool}) FAILED ===\n{exc}")
        return

    header = f"=== Bibliography ({tool})"
    if rc != 0:
        header += f" exited {rc}"
    out_chunks.append(f"{header} ===\n{out}")
    err_chunks.append(err)


# ─── Public Compilation API ──────────────────────────────────────────────────

def compile_project(
    workspace: Path,
    main_tex: str,
    engine: EngineType = DEFAULT_ENGINE,
    timeout: int = COMPILE_TIMEOUT,
) -> CompilationResult:
    """Compile a LaTeX project and return a structured result.

    Tries latexmk first and falls back to manual passes ONLY when latexmk itself
    could not run (missing binary / Perl) – never merely because the document
    had errors. Build artifacts are cleaned first so ``success`` reflects this
    run, not a leftover PDF.
    """
    start_time = time.monotonic()
    deadline = _Deadline(timeout)

    tex_path = workspace / main_tex
    if not tex_path.exists():
        return CompilationResult(
            success=False, pdf_path=None, log_path=None, parsed_log=None,
            summary=f"Main .tex file not found: '{main_tex}'",
            duration_seconds=0, engine=engine,
        )

    # latexmk runs in the directory that contains the main .tex.
    tex_dir = tex_path.parent
    tex_filename = tex_path.name
    base_name = tex_path.stem

    env = _build_env(workspace)
    use_latexmk = _has_binary("latexmk")

    returncode = -1
    stdout = stderr = ""

    try:
        # Compile, retrying after an on-demand package install if that helped.
        for attempt in range(_MAX_INSTALL_RETRIES + 1):
            _clean_artifacts(tex_dir, base_name, workspace)

            if use_latexmk:
                returncode, stdout, stderr = _compile_with_latexmk(
                    tex_filename, tex_dir, engine, deadline, env
                )
                # Fall back to manual passes ONLY on a latexmk infrastructure
                # failure (no log produced or a Perl/executable error), not on
                # ordinary document errors.
                if returncode != 0 and _latexmk_infra_failed(
                    tex_dir, base_name, stderr
                ):
                    returncode, stdout, stderr = _compile_manual_passes(
                        tex_filename, tex_dir, engine, deadline, env
                    )
                    use_latexmk = False
            else:
                returncode, stdout, stderr = _compile_manual_passes(
                    tex_filename, tex_dir, engine, deadline, env
                )

            # If the PDF is present we are done. Otherwise, see whether a
            # missing package explains it and, if so, install and retry.
            if _fresh_pdf(tex_dir, base_name) or deadline.expired():
                break
            raw = _read_text_lossless(tex_dir / f"{base_name}.log") or (stdout + stderr)
            if attempt < _MAX_INSTALL_RETRIES and _install_missing_packages(raw, deadline):
                continue
            break

    except TimeoutError as e:
        return CompilationResult(
            success=False, pdf_path=None, log_path=None, parsed_log=None,
            summary=str(e), duration_seconds=time.monotonic() - start_time,
            returncode=returncode, engine=engine,
        )
    except EnvironmentError as e:
        return CompilationResult(
            success=False, pdf_path=None, log_path=None, parsed_log=None,
            summary=str(e), duration_seconds=time.monotonic() - start_time,
            returncode=returncode, engine=engine,
        )

    duration = time.monotonic() - start_time

    # ── Read and parse the .log ──────────────────────────────────────────────
    log_file = tex_dir / f"{base_name}.log"
    raw_log = _read_text_lossless(log_file) if log_file.exists() else (stdout + "\n" + stderr)
    parsed = parse_log(raw_log)
    summary = get_log_summary(parsed)

    # ── Decide success: a fresh, non-empty PDF exists (artifacts were cleaned) ─
    pdf_file = tex_dir / f"{base_name}.pdf"
    success = pdf_file.exists() and pdf_file.stat().st_size > 0
    if not success:
        # Some setups drop the PDF at the workspace root instead.
        alt_pdf = workspace / f"{base_name}.pdf"
        if alt_pdf.exists() and alt_pdf.stat().st_size > 0:
            pdf_file, success = alt_pdf, True

    if not success:
        summary = f"Compilation failed (exit code {returncode}). " + summary

    return CompilationResult(
        success=success,
        pdf_path=pdf_file if success else None,
        log_path=log_file if log_file.exists() else None,
        parsed_log=parsed,
        summary=summary,
        duration_seconds=duration,
        returncode=returncode,
        engine=engine,
    )


def _fresh_pdf(tex_dir: Path, base_name: str) -> bool:
    """True if a non-empty PDF for ``base_name`` exists after a compile."""
    for folder in (tex_dir,):
        pdf = folder / f"{base_name}.pdf"
        if pdf.exists() and pdf.stat().st_size > 0:
            return True
    return False


def _latexmk_infra_failed(tex_dir: Path, base_name: str, stderr: str) -> bool:
    """Distinguish "latexmk could not run" from "the document has errors".

    Only the former should trigger the manual fallback. Signs of an
    infrastructure failure: no .log was produced at all, or stderr mentions a
    Perl/executable problem.
    """
    if not (tex_dir / f"{base_name}.log").exists():
        return True
    return bool(re.search(r"perl|Can't locate|is not recognized|command not found",
                          stderr, re.IGNORECASE))


def check_latex_available() -> dict:
    """Report which LaTeX tools are available (used by /api/status and run.py)."""
    tools = ["pdflatex", "xelatex", "lualatex", "latexmk", "bibtex", "biber", "tlmgr"]
    result = {}
    for tool in tools:
        try:
            path = _resolve_binary(tool)
            result[tool] = {"available": True, "path": path}
        except EnvironmentError:
            result[tool] = {"available": False, "path": None}
    return result
