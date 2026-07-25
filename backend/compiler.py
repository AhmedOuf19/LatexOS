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

# Missing-file markers used to drive on-demand package installation. We capture
# the bare filename from the common "not found" phrasings (LaTeX, kpathsea and
# bibtex), then filter out user assets in _installable_missing_files() so a
# missing figure or a forgotten \input is never mistaken for a package.
_RE_MISSING_FILE = re.compile(
    r"File `([^']+)' not found"
    r"|Cannot find file `([^']+)'"
    r"|couldn't open style file (\S+)"     # bibtex: I couldn't open style file IEEEtran.bst
    r"|open file (\S+) for reading",
)

# Extensions that are USER assets (or user source), never installable packages.
_NON_PACKAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps", ".svg", ".tif", ".tiff",
    ".bmp", ".gif", ".csv", ".dat", ".txt", ".bib",
    ".tex",  # a missing .tex is almost always a forgotten \input, not a package
}

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

    def extend(self, seconds: float) -> None:
        """Push the deadline out by ``seconds`` – used to give the compile back
        the wall-clock spent downloading packages, so on-demand installs never
        starve the final recompile."""
        self._end += max(0.0, seconds)


# ─── Binary Resolution ───────────────────────────────────────────────────────

def _resolve_binary(name: str) -> str:
    """Return the full path to a LaTeX binary.

    Prefers ``LATEX_BIN_PATH`` from config (the folder-local or detected distro)
    and falls back to PATH. Raises ``EnvironmentError`` if the tool is missing.
    """
    # Try the Windows executable/script extensions. TinyTeX ships some tools as
    # .bat scripts (notably tlmgr.bat), so .exe alone is not enough — missing
    # .bat here silently disables on-demand package installation.
    if LATEX_BIN_PATH:
        for candidate in (f"{name}.exe", f"{name}.bat", f"{name}.cmd", name):
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
# tlmgr operations hit the network, so they get their own fixed timeouts and do
# NOT draw down the compile deadline (the caller adds the elapsed install time
# back via _Deadline.extend, so the final recompile keeps its full budget).
_TLMGR_SEARCH_TIMEOUT = 45
_TLMGR_INSTALL_TIMEOUT = 180


def _tool_env() -> dict:
    """Environment with the LaTeX bin dir first on PATH (for tlmgr/kpsewhich)."""
    env = os.environ.copy()
    if LATEX_BIN_PATH:
        env["PATH"] = LATEX_BIN_PATH + os.pathsep + env.get("PATH", "")
    return env


def _installable_missing_files(raw_log: str) -> list[str]:
    """Extract missing files from a log that are plausibly installable packages
    (i.e. not user images/data or a forgotten \\input .tex)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _RE_MISSING_FILE.finditer(raw_log):
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        name = name.strip().strip("'\"")
        ext = Path(name).suffix.lower()
        if not ext or ext in _NON_PACKAGE_EXTS or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _kpsewhich_finds(fname: str, env: dict) -> bool:
    """True if kpathsea can now locate ``fname`` (proof an install worked)."""
    if not _has_binary("kpsewhich"):
        return False
    try:
        rc, out, _err = _run([_resolve_binary("kpsewhich"), fname], Path.cwd(), 20, env)
    except (TimeoutError, OSError, EnvironmentError):
        return False
    return rc == 0 and bool(out.strip())


def _install_missing_packages(
    raw_log: str, attempted: set[str], failures: list[str]
) -> bool:
    """Install packages a failed compile reported as missing.

    Only runs for TinyTeX/TeX Live (via ``tlmgr``); MiKTeX installs on its own.
    Installs EVERY newly-missing resource (not a fixed cap), records already-
    attempted files in ``attempted`` to guarantee forward progress, and verifies
    each install with kpsewhich — because ``tlmgr.bat`` always exits 0, even for
    a package that does not exist. Human-readable problems are appended to
    ``failures``. Returns True only if at least one file genuinely became
    resolvable, so the caller knows a recompile is worthwhile.
    """
    if not AUTO_INSTALL_PACKAGES or not _has_binary("tlmgr"):
        return False

    fresh = [f for f in _installable_missing_files(raw_log) if f not in attempted]
    if not fresh:
        return False

    tlmgr = _resolve_binary("tlmgr")
    env = _tool_env()
    installed_any = False

    for fname in fresh:
        attempted.add(fname)  # never retry the same file (forward progress)
        pkg = _tlmgr_package_for_file(tlmgr, fname, env)
        if not pkg:
            failures.append(
                f"'{fname}' is missing and no package could be found for it "
                f"(the package repository may be unreachable)."
            )
            continue
        try:
            _run([tlmgr, "install", pkg], Path.cwd(), _TLMGR_INSTALL_TIMEOUT, env)
        except (TimeoutError, OSError):
            failures.append(f"Installing package '{pkg}' for '{fname}' timed out.")
            continue
        # tlmgr.bat's exit code is unreliable; confirm the file now resolves.
        if _kpsewhich_finds(fname, env):
            installed_any = True
        else:
            failures.append(f"Package '{pkg}' did not provide '{fname}' after install.")
    return installed_any


def _tlmgr_package_for_file(tlmgr: str, fname: str, env: dict) -> str | None:
    """Map a missing file to the tlmgr package that provides it.

    Returns the package name only if the search positively resolves one. We do
    NOT fall back to the filename stem, because that installs the wrong (or a
    non-existent) package — e.g. tikz.sty is provided by 'pgf', not 'tikz'.
    """
    try:
        rc, out, _err = _run(
            [tlmgr, "search", "--global", "--file", f"/{fname}"],
            Path.cwd(), _TLMGR_SEARCH_TIMEOUT, env,
        )
    except (TimeoutError, OSError):
        return None
    if rc != 0:
        return None
    # tlmgr prints "pkgname:" header lines above each matching file path.
    for line in out.splitlines():
        line = line.strip()
        if line.endswith(":") and "/" not in line and not line.startswith("tlmgr"):
            return line[:-1]
    return None


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

    # Pass 1 – generates the .aux / .bcf / .idx / .glo that tell us which
    # auxiliary tools (bibliography, index, glossary) to run.
    if deadline.expired():
        return returncode, "", ""
    run_engine("Pass 1")

    # Bibliography, then index/glossary – the things latexmk would do for us.
    _run_bibliography(base_name, workspace, deadline, env, out_chunks, err_chunks)
    _run_index_glossary(base_name, workspace, deadline, env, out_chunks, err_chunks)

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
    elif r"\bibdata" in aux_text and _has_binary("bibtex"):
        # \bibdata (from \bibliography{...}) is the real signal that bibtex is
        # needed. \citation alone means a hand-written thebibliography block,
        # which needs no bibtex run (running it would just error on "no \bibdata").
        tool = "bibtex"

    if not tool or deadline.expired():
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


def _run_index_glossary(
    base_name: str, workspace: Path, deadline: _Deadline, env: dict,
    out_chunks: list[str], err_chunks: list[str],
) -> None:
    """Run makeindex / makeglossaries for the manual path if the engine produced
    an index (.idx) or glossary (.glo) file – mirroring what latexmk does
    automatically. Each tool is resolved with the same .exe/.bat-aware resolver;
    if the required tool is absent from a minimal TinyTeX, that is noted rather
    than silently producing an empty index/glossary.
    """
    jobs = [
        (".idx", "makeindex", [base_name + ".idx"]),
        (".glo", "makeglossaries", [base_name]),
        (".nlo", "makeindex", ["-s", "nomencl.ist", "-o", base_name + ".nls", base_name + ".nlo"]),
    ]
    for ext, tool, args in jobs:
        if not (workspace / f"{base_name}{ext}").exists():
            continue
        if deadline.expired():
            return
        if not _has_binary(tool):
            out_chunks.append(f"=== {tool} not available (index/glossary may be incomplete) ===")
            continue
        try:
            rc, out, err = _run([_resolve_binary(tool)] + args, workspace,
                                 deadline.remaining(), env)
            out_chunks.append(f"=== {tool}{' exited ' + str(rc) if rc else ''} ===\n{out}")
            err_chunks.append(err)
        except (TimeoutError, EnvironmentError) as exc:
            out_chunks.append(f"=== {tool} FAILED ===\n{exc}")


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
    attempted_installs: set[str] = set()   # files we've already tried to install
    install_failures: list[str] = []       # human-readable install problems
    pre_pdf_mtime: float | None = None      # PDF mtime BEFORE the last compile

    try:
        # Compile, retrying after an on-demand package install if that helped.
        for attempt in range(_MAX_INSTALL_RETRIES + 1):
            if deadline.expired():
                break
            _clean_artifacts(tex_dir, base_name, workspace)
            # Record whether a PDF survived the clean (e.g. locked by a viewer)
            # so a stale, undeletable PDF cannot later be mistaken for fresh.
            pdf_before = tex_dir / f"{base_name}.pdf"
            pre_pdf_mtime = pdf_before.stat().st_mtime if pdf_before.exists() else None

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

            # If a fresh PDF is present we are done. Otherwise, see whether
            # missing packages explain it and, if so, install them and retry.
            if _fresh_pdf(tex_dir, base_name, pre_pdf_mtime) or deadline.expired():
                break
            if attempt >= _MAX_INSTALL_RETRIES:
                break
            # Feed the engine .log AND the bibtex/biber .blg (missing .bst errors
            # live there, not in the .log) into the installer.
            raw = _read_text_lossless(tex_dir / f"{base_name}.log")
            blg = _read_text_lossless(tex_dir / f"{base_name}.blg")
            raw = "\n".join(x for x in (raw, blg, stdout, stderr) if x)
            # tlmgr hits the network; don't let that time count against the
            # compile budget – extend the deadline by the install wall-time.
            t0 = time.monotonic()
            installed = _install_missing_packages(raw, attempted_installs, install_failures)
            deadline.extend(time.monotonic() - t0)
            if not installed:
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

    # ── Decide success: a FRESH, non-empty PDF exists (see _fresh_pdf) ────────
    pdf_file = tex_dir / f"{base_name}.pdf"
    success = _fresh_pdf(tex_dir, base_name, pre_pdf_mtime)
    if not success:
        # Some setups drop the PDF at the workspace root instead.
        alt_pdf = workspace / f"{base_name}.pdf"
        if alt_pdf.exists() and alt_pdf.stat().st_size > 0:
            pdf_file, success = alt_pdf, True

    if not success:
        summary = f"Compilation failed (exit code {returncode}). " + summary
        # Surface any on-demand install problems so a missing package that
        # could not be fetched is not just a bare "File not found".
        if install_failures:
            summary += " Package install issues: " + " ".join(dict.fromkeys(install_failures))

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


def _fresh_pdf(tex_dir: Path, base_name: str, pre_mtime: float | None = None) -> bool:
    """True if a non-empty PDF for ``base_name`` was produced by THIS run.

    Normally artifacts are cleaned first, so mere existence proves freshness. But
    if the previous PDF could not be deleted (locked by a viewer/AV scanner),
    ``pre_mtime`` is its pre-compile timestamp; we then require the PDF on disk to
    be strictly newer, so a stale locked PDF never reports a failed compile as a
    success.
    """
    pdf = tex_dir / f"{base_name}.pdf"
    if not (pdf.exists() and pdf.stat().st_size > 0):
        return False
    if pre_mtime is not None and pdf.stat().st_mtime <= pre_mtime:
        return False
    return True


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
