"""
config.py – Centralized configuration for LaTeX Studio.

Everything that can be tuned lives here so there is exactly one place to look.
Every value can be overridden with an environment variable, which is how the
launcher scripts and advanced users customise the app without editing code.

Design goals reflected in this file
-----------------------------------
* **Safe by default.** Shell-escape (arbitrary command execution from a .tex
  file) is OFF unless the user explicitly opts in. See ``ALLOW_SHELL_ESCAPE``.
* **Portable / self-contained.** ``_find_latex_bin()`` looks *inside the project
  folder* first (``./tinytex``) before falling back to a system MiKTeX/TeX Live,
  so a folder-local LaTeX distribution is preferred.
* **Bounded.** Upload size, ZIP-extraction size, member count and log-read size
  all have explicit limits so a single request cannot exhaust memory or disk.

Everything here is evaluated **once, at import time**. Changing an environment
variable (or installing LaTeX) while the server is running has no effect until
it is restarted — which is also what makes these values safe to read as plain
module constants from anywhere in the backend.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, get_args

# ─── Base Paths ───────────────────────────────────────────────────────────────
# BASE_DIR is the project root (the folder that contains backend/, frontend/…).
BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"          # per-session workspaces (generated)
FRONTEND_DIR = BASE_DIR / "frontend"       # static UI served to the browser
LOGS_DIR = BASE_DIR / "logs"               # application / launcher logs (generated)


def _get_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable.

    Accepts the usual truthy spellings (1/true/yes/on, case-insensitive).
    Anything else – including an unset variable – yields ``default``.

    Note the asymmetry: an unrecognised value reads as *false*, not as
    ``default``. For the security-sensitive flags below that is the safe way
    round — a mistyped ``LATEX_ALLOW_SHELL_ESCAPE=treu`` leaves shell-escape off.
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to ``default`` on any
    parse error so a typo in the environment can never crash startup."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ─── LaTeX Engines (single source of truth) ──────────────────────────────────
# EngineType is used for type-checking; ENGINES is the runtime tuple derived
# from it. Defining the list once means adding an engine is a one-line change
# and the API validation, the flag map and the config default can never drift.
EngineType = Literal["pdflatex", "xelatex", "lualatex"]
ENGINES: tuple[str, ...] = get_args(EngineType)

# Default engine, validated against the allowed set. An invalid LATEX_ENGINE
# value falls back to pdflatex instead of silently failing every compile.
_requested_engine = os.getenv("LATEX_ENGINE", "pdflatex").strip().lower()
DEFAULT_ENGINE: str = _requested_engine if _requested_engine in ENGINES else "pdflatex"

# ─── Compilation Settings ─────────────────────────────────────────────────────
# Hard wall-clock budget for a single compile request (all passes combined).
# Generous by default so large books/theses and slow first-time compiles finish;
# time spent auto-installing packages is added back on top of this. Raise it
# further with LATEX_TIMEOUT if you compile very large documents.
COMPILE_TIMEOUT: int = _get_int("LATEX_TIMEOUT", 600)  # 10 minutes

# SECURITY: shell-escape lets a .tex run arbitrary OS commands via \write18.
# It is required by a few packages (minted, some svg/gnuplot workflows) but is a
# remote-code-execution vector for untrusted documents, so it is OFF by default.
# Turn it on only for documents you trust:  set LATEX_ALLOW_SHELL_ESCAPE=1
ALLOW_SHELL_ESCAPE: bool = _get_bool("LATEX_ALLOW_SHELL_ESCAPE", False)

# When a package is missing, attempt an on-demand install (tlmgr for TinyTeX /
# TeX Live). MiKTeX has its own built-in auto-installer, so this is mainly for
# the portable TinyTeX distribution. Disable with LATEX_AUTO_INSTALL=0.
AUTO_INSTALL_PACKAGES: bool = _get_bool("LATEX_AUTO_INSTALL", True)

# ─── File Upload Limits ───────────────────────────────────────────────────────
# Generous defaults for real-world projects with large images/PDF assets; all
# overridable via the environment.
MAX_UPLOAD_SIZE_MB: int = _get_int("MAX_UPLOAD_MB", 500)
MAX_UPLOAD_SIZE_BYTES: int = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# A ZIP can decompress to far more than its own size ("zip bomb"). Cap the total
# extracted bytes and the number of members independently of the archive size.
# Defaults to 4x the upload cap (so 2 GB with the 500 MB default).
MAX_EXTRACTED_SIZE_BYTES: int = _get_int(
    "MAX_EXTRACTED_MB", MAX_UPLOAD_SIZE_MB * 4
) * 1024 * 1024
MAX_ZIP_MEMBERS: int = _get_int("MAX_ZIP_MEMBERS", 10000)

# A runaway document can write a multi-GB .log; only read this many bytes of it.
MAX_LOG_READ_BYTES: int = _get_int("MAX_LOG_READ_MB", 32) * 1024 * 1024

# Whitelisted file extensions (case-insensitive). Everything else is rejected on
# upload and skipped during ZIP extraction.
ALLOWED_EXTENSIONS: set[str] = {
    # LaTeX source
    ".tex", ".bib", ".bst", ".cls", ".sty", ".ist", ".dtx", ".ins",
    # Images
    ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps", ".svg",
    ".tif", ".tiff", ".bmp", ".gif",
    # Fonts
    ".ttf", ".otf", ".pfb", ".pfm",
    # Data / includes
    ".csv", ".dat", ".txt", ".md",
    # Archive (ZIP of a full project)
    ".zip",
}


def is_allowed_extension(filename: str) -> bool:
    """True if ``filename`` has a whitelisted extension (case-insensitive)."""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


# ─── Session Settings ─────────────────────────────────────────────────────────
# Sessions untouched for this long are eligible for cleanup. The clock is reset
# on every request to a session, so this is an *inactivity* timeout; a generous
# default keeps a project around across long editing breaks.
SESSION_TTL_SECONDS: int = _get_int("SESSION_TTL", 21600)  # 6 hours
# How often the background cleaner runs while the server is up (0 disables it;
# a sweep still runs at startup).
SESSION_GC_INTERVAL_SECONDS: int = _get_int("SESSION_GC_INTERVAL", 900)  # 15 min

# ─── Networking ───────────────────────────────────────────────────────────────
# Bind to loopback by default – this app has no authentication and must never be
# exposed to a network without a deliberate, informed choice.
HOST: str = os.getenv("LATEX_HOST", "127.0.0.1")
PORT: int = _get_int("LATEX_PORT", 8000)

# ─── LaTeX Distribution Auto-Detection ───────────────────────────────────────
def _find_latex_bin() -> str | None:
    """Locate the directory containing ``pdflatex``.

    Priority:
      1. ``LATEX_BIN_PATH`` environment override (set by the launcher).
      2. A folder-local TinyTeX / TeX Live under the project directory
         (this is what makes a portable, self-contained install work).
      3. Common system MiKTeX / TeX Live locations on Windows.
      4. Whatever ``pdflatex`` is already on PATH.
    Returns the directory as a string, or ``None`` if nothing was found.
    """
    exe = "pdflatex.exe" if os.name == "nt" else "pdflatex"

    # 1. Explicit override wins.
    env_path = os.getenv("LATEX_BIN_PATH")
    if env_path and Path(env_path).is_dir():
        return env_path

    # 2. Folder-local distributions (portable install). Checked first so a
    #    project copied to a USB stick uses its own bundled LaTeX.
    local_candidates = [
        BASE_DIR / "tinytex" / "bin" / "windows",
        BASE_DIR / "tinytex" / "bin" / "win32",
        BASE_DIR / "tinytex" / "bin" / "x86_64-w64-mingw32",
        BASE_DIR / "texlive" / "bin" / "windows",
        # POSIX (for developers / CI on Linux/macOS)
        BASE_DIR / "tinytex" / "bin" / "x86_64-linux",
        BASE_DIR / ".TinyTeX" / "bin" / "x86_64-linux",
    ]

    # 3. Common system installs.
    username = os.getenv("USERNAME", "")
    system_candidates = [
        Path(rf"C:\Users\{username}\AppData\Local\Programs\MiKTeX\miktex\bin\x64"),
        Path(r"C:\Program Files\MiKTeX\miktex\bin\x64"),
        Path(r"C:\Program Files (x86)\MiKTeX\miktex\bin"),
        Path(r"C:\Program Files\MiKTeX 2.9\miktex\bin\x64"),
        Path(r"C:\texlive\2026\bin\windows"),
        Path(r"C:\texlive\2025\bin\windows"),
        Path(r"C:\texlive\2024\bin\windows"),
        Path(r"C:\texlive\2023\bin\windows"),
        Path(r"C:\texlive\2022\bin\win32"),
    ]

    # Order matters: local_candidates first, so a portable copy always beats a
    # system install rather than the other way round.
    for candidate_dir in local_candidates + system_candidates:
        if candidate_dir.is_dir() and (candidate_dir / exe).exists():
            return str(candidate_dir)

    # 4. Fall back to PATH. Imported here because only this last-resort branch
    #    needs shutil.
    import shutil

    pdflatex_on_path = shutil.which("pdflatex")
    if pdflatex_on_path:
        return str(Path(pdflatex_on_path).parent)

    return None


# Resolved once at import. Installing LaTeX after the server has started
# therefore requires a restart before the app can see it.
LATEX_BIN_PATH: str | None = _find_latex_bin()

# ─── Logging ──────────────────────────────────────────────────────────────────
# Applied by main.py via logging.basicConfig(level=...). Invalid values fall
# back to INFO in main.py, so a bad LOG_LEVEL never crashes startup.
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
