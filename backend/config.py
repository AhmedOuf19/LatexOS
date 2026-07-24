"""
config.py – Centralized configuration for the LaTeX-to-PDF web application.
All paths and limits are defined here. Override via environment variables if needed.
"""

import os
import shutil
from pathlib import Path

# ─── Base Paths ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
FRONTEND_DIR = BASE_DIR / "frontend"

# ─── Compilation Settings ─────────────────────────────────────────────────────
# Seconds before a compilation process is forcibly killed
COMPILE_TIMEOUT: int = int(os.getenv("LATEX_TIMEOUT", "120"))

# Default LaTeX engine. Options: "pdflatex", "xelatex", "lualatex"
DEFAULT_ENGINE: str = os.getenv("LATEX_ENGINE", "pdflatex")

# ─── File Upload Limits ───────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_SIZE_BYTES: int = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# Whitelisted file extensions (case-insensitive)
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
    # Archive (ZIP of full project)
    ".zip",
}

# ─── Session Settings ─────────────────────────────────────────────────────────
# Sessions older than this many seconds are eligible for cleanup
SESSION_TTL_SECONDS: int = int(os.getenv("SESSION_TTL", "3600"))  # 1 hour

# ─── MiKTeX / TeX Live Auto-Detection ────────────────────────────────────────
def _find_latex_bin() -> str | None:
    """
    Try to find the LaTeX binary directory on Windows.
    Checks common MiKTeX and TeX Live install locations, then falls back to PATH.
    """
    # 1. Check environment override first
    env_path = os.getenv("LATEX_BIN_PATH")
    if env_path and Path(env_path).is_dir():
        return env_path

    # 2. Common MiKTeX locations on Windows
    candidate_dirs = [
        # Per-user MiKTeX installs (most common)
        r"C:\Users\{}\AppData\Local\Programs\MiKTeX\miktex\bin\x64".format(
            os.getenv("USERNAME", "")
        ),
        # System-wide MiKTeX installs
        r"C:\Program Files\MiKTeX\miktex\bin\x64",
        r"C:\Program Files (x86)\MiKTeX\miktex\bin",
        r"C:\Program Files\MiKTeX 2.9\miktex\bin\x64",
        # TeX Live on Windows (check recent years first)
        r"C:\texlive\2026\bin\windows",
        r"C:\texlive\2025\bin\windows",
        r"C:\texlive\2024\bin\windows",
        r"C:\texlive\2023\bin\windows",
        r"C:\texlive\2022\bin\win32",
    ]

    for d in candidate_dirs:
        p = Path(d)
        if p.is_dir() and (p / "pdflatex.exe").exists():
            return str(p)

    # 3. Fall back to whatever is on PATH
    pdflatex = shutil.which("pdflatex")
    if pdflatex:
        return str(Path(pdflatex).parent)

    return None


LATEX_BIN_PATH: str | None = _find_latex_bin()

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
