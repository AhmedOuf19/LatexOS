"""
run.py – One-command startup and pre-flight check for LaTeX Studio.

Usage:
    python run.py                 → start on http://127.0.0.1:8000
    python run.py --port 9000     → custom port
    python run.py --check         → run pre-flight checks only, then exit
    python run.py --open-browser  → open the browser once the server is up
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure the project root is importable when run as a script.
sys.path.insert(0, os.path.dirname(__file__))

from backend import __version__


def check_prerequisites() -> bool:
    """Verify Python, dependencies, LaTeX tools and directories. Returns ok."""
    print("=" * 60)
    print(f"  LaTeX Studio {__version__} - Pre-flight Check")
    print("=" * 60)

    all_ok = True

    # 1. Python version (3.10+).
    py = sys.version_info
    py_ok = py >= (3, 10)
    print(f"{'[OK]  ' if py_ok else '[FAIL]'} Python {py.major}.{py.minor}.{py.micro}"
          f" {'' if py_ok else '(need 3.10+)'}")
    all_ok &= py_ok

    # 2. Web framework.
    try:
        import fastapi
        import uvicorn
        print(f"[OK]   FastAPI {fastapi.__version__}, uvicorn {uvicorn.__version__}")
    except ImportError as e:
        print(f"[FAIL] Missing dependency: {e}\n  Run: pip install -r requirements.txt")
        all_ok = False

    # 3. LaTeX tools.
    try:
        from backend.compiler import check_latex_available
        status = check_latex_available()

        if status.get("pdflatex", {}).get("available"):
            path = status["pdflatex"]["path"]
            print(f"[OK]   pdflatex: {path}")
            _configure_miktex_autoinstall(path)
        else:
            print("[FAIL] pdflatex NOT found — install TinyTeX / MiKTeX / TeX Live.")
            all_ok = False

        for tool in ("latexmk", "xelatex", "lualatex", "bibtex", "biber", "tlmgr"):
            if status.get(tool, {}).get("available"):
                print(f"[OK]   {tool}: available")
    except Exception as e:  # never let the check itself crash startup
        print(f"[FAIL] LaTeX check failed: {e}")
        all_ok = False

    # 4. Directories.
    from backend.config import ALLOW_SHELL_ESCAPE, FRONTEND_DIR, UPLOAD_DIR
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[OK]   Upload directory: {UPLOAD_DIR}")
    if FRONTEND_DIR.exists():
        print(f"[OK]   Frontend directory: {FRONTEND_DIR}")
    else:
        print(f"[FAIL] Frontend directory not found: {FRONTEND_DIR}")
        all_ok = False

    if ALLOW_SHELL_ESCAPE:
        print("[WARN] shell-escape is ENABLED — only compile documents you trust.")

    print("=" * 60)
    print("  All checks passed!" if all_ok else "  Some checks FAILED. See messages above.")
    print("=" * 60)
    return all_ok


def _configure_miktex_autoinstall(pdflatex_path: str) -> None:
    """Tell MiKTeX to install missing packages silently (the correct key is
    ``[MPM]AutoInstall``). No-op for non-MiKTeX distributions."""
    if sys.platform != "win32" or "miktex" not in pdflatex_path.lower():
        return
    import subprocess
    try:
        subprocess.run(
            ["initexmf", "--set-config-value", "[MPM]AutoInstall=1"],
            capture_output=True, timeout=10,
        )
        print("[OK]   MiKTeX configured for silent auto-install")
    except Exception as e:
        print(f"[WARN] Could not configure MiKTeX auto-install: {e}")


def main() -> None:
    from backend.config import HOST, PORT

    parser = argparse.ArgumentParser(description="LaTeX Studio - startup")
    parser.add_argument("--port", type=int, default=PORT, help=f"port (default: {PORT})")
    parser.add_argument("--host", type=str, default=HOST, help=f"host (default: {HOST})")
    parser.add_argument("--check", action="store_true", help="run pre-flight checks only")
    parser.add_argument("--reload", action="store_true", help="auto-reload (development)")
    parser.add_argument("--open-browser", action="store_true", help="open the browser on start")
    args = parser.parse_args()

    ok = check_prerequisites()
    if args.check:
        sys.exit(0 if ok else 1)
    if not ok:
        print("\n[WARN] Starting anyway; some features may not work.\n")

    print(f"\n[START] LaTeX Studio on http://{args.host}:{args.port}")
    print(f"   API docs: http://{args.host}:{args.port}/docs")
    print("   Press Ctrl+C to stop.\n")

    if args.open_browser:
        import threading
        import webbrowser
        # Open after a short delay so uvicorn has time to bind the port.
        threading.Timer(2.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()

    import uvicorn
    uvicorn.run("backend.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
