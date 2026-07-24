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
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
COMPILE_TIMEOUT: int = _get_int("LATEX_TIMEOUT", 120)

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
MAX_UPLOAD_SIZE_MB: int = _get_int("MAX_UPLOAD_MB", 100)
MAX_UPLOAD_SIZE_BYTES: int = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# A ZIP can decompress to far more than its own size ("zip bomb"). Cap the total
# extracted bytes and the number of members independently of the archive size.
MAX_EXTRACTED_SIZE_BYTES: int = _get_int(
    "MAX_EXTRACTED_MB", MAX_UPLOAD_SIZE_MB * 4
) * 1024 * 1024
MAX_ZIP_MEMBERS: int = _get_int("MAX_ZIP_MEMBERS", 2000)

# A runaway document can write a multi-GB .log; only read this many bytes of it.
MAX_LOG_READ_BYTES: int = _get_int("MAX_LOG_READ_MB", 8) * 1024 * 1024

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
# Sessions untouched for this long are eligible for cleanup.
SESSION_TTL_SECONDS: int = _get_int("SESSION_TTL", 3600)  # 1 hour
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

    for d in local_candidates + system_candidates:
        if d.is_dir() and (d / exe).exists():
            return str(d)

    # 4. Fall back to PATH.
    import shutil

    found = shutil.which("pdflatex")
    if found:
        return str(Path(found).parent)

    return None


LATEX_BIN_PATH: str | None = _find_latex_bin()

# ─── Logging ──────────────────────────────────────────────────────────────────
# Applied by main.py via logging.basicConfig(level=...). Invalid values fall
# back to INFO in main.py, so a bad LOG_LEVEL never crashes startup.
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
