"""
run.py – One-command startup and pre-flight check for LaTeX Studio.

This is the entry point a non-technical user reaches (through the launcher in
``scripts/run.ps1`` and the ``.bat`` shortcut) as well as the one a developer
runs by hand. Its real job is to **fail legibly**: when LaTeX or a Python
dependency is missing, the user should see a short list of what is wrong
instead of a traceback.

Usage:
    python run.py                 → start on http://127.0.0.1:8000
    python run.py --port 9000     → custom port
    python run.py --check         → run pre-flight checks only, then exit
    python run.py --open-browser  → open the browser once the server is up

Design decisions a reader would otherwise have to reverse-engineer
------------------------------------------------------------------
* **Checks warn; they do not block.** A failed check aborts only when
  ``--check`` was passed. Otherwise the server starts anyway, because a user
  with no LaTeX installed can still open the UI and read the problem there —
  much less alarming than an app that refuses to launch. The ``--check`` exit
  code (0 = healthy, 1 = something failed) is the scriptable contract; the
  printed text is not.
* **Backend imports live inside the functions, not at module top.** Importing
  ``backend.config`` scans the filesystem for a LaTeX distribution and
  ``backend.compiler`` pulls in the whole compile stack — either can fail on a
  half-installed machine. Deferring them means the earlier checks have already
  printed their results by the time anything can go wrong, so the user sees how
  far the install got instead of a bare ImportError. Only ``backend/__init__``
  is imported at module level, and it is kept dependency-free for exactly this
  reason.
* **The command line wins over the environment.** Flag defaults are read from
  ``backend.config``, which itself reads environment variables, giving the
  precedence order: flag > environment variable > built-in default.
"""

from __future__ import annotations

import argparse
import os
import sys

# Put the project root first on sys.path so ``backend`` resolves no matter which
# working directory the launcher scripts invoke this file from. Every backend
# import in this module depends on this line having already run, which is why
# they sit below it (and, for the rest, inside functions).
sys.path.insert(0, os.path.dirname(__file__))

from backend import __version__


# ─── Pre-flight checks ────────────────────────────────────────────────────────


def check_prerequisites() -> bool:
    """Print a pass/fail report of everything the app needs and return whether
    all of it is present.

    The checks run cheapest-and-most-fundamental first so that the first
    ``[FAIL]`` a user sees is usually the root cause rather than a downstream
    symptom of it:

    1. **Python version** – reported in plain language here, because on an
       unsupported interpreter the alternative is whatever import-time error
       happens to fire first, which tells a non-technical user nothing.
    2. **FastAPI / uvicorn** – separates "the Python dependencies are not
       installed" from "LaTeX is not installed". The two look similar to a
       beginner but are fixed in completely different ways.
    3. **LaTeX toolchain** – ``pdflatex`` is the only hard requirement. The
       others (latexmk, biber, …) are listed for information only, so a user
       can see *why* their bibliography or cross-references never resolved.
    4. **Directories** – ``uploads/`` is generated, so it is created on the
       spot; a missing ``frontend/`` means a truncated download or a broken
       install and is a genuine failure.

    Check 3 is the one wrapped in a bare ``except Exception``: probing for LaTeX
    binaries is by far the most failure-prone step, and a diagnostic that dies
    while diagnosing would defeat its own purpose.
    """
    print("=" * 60)
    print(f"  LaTeX Studio {__version__} - Pre-flight Check")
    print("=" * 60)

    all_checks_passed = True

    # 1. Python version (3.10+).
    python_version = sys.version_info
    is_python_supported = python_version >= (3, 10)
    print(f"{'[OK]  ' if is_python_supported else '[FAIL]'}"
          f" Python {python_version.major}.{python_version.minor}.{python_version.micro}"
          f" {'' if is_python_supported else '(need 3.10+)'}")
    all_checks_passed &= is_python_supported

    # 2. Web framework.
    try:
        import fastapi
        import uvicorn
        print(f"[OK]   FastAPI {fastapi.__version__}, uvicorn {uvicorn.__version__}")
    except ImportError as e:
        print(f"[FAIL] Missing dependency: {e}\n  Run: pip install -r requirements.txt")
        all_checks_passed = False

    # 3. LaTeX tools.
    try:
        from backend.compiler import check_latex_available
        tool_status = check_latex_available()

        if tool_status.get("pdflatex", {}).get("available"):
            pdflatex_path = tool_status["pdflatex"]["path"]
            print(f"[OK]   pdflatex: {pdflatex_path}")
            # Side effect inside a "check": the MiKTeX tweak below is gated on
            # the resolved pdflatex path, and here is where that path is known.
            _configure_miktex_autoinstall(pdflatex_path)
        else:
            print("[FAIL] pdflatex NOT found — install TinyTeX / MiKTeX / TeX Live.")
            all_checks_passed = False

        # Reported but never fatal - a document that needs none of these still
        # compiles fine, and saying "missing" would read as an error.
        for tool in ("latexmk", "xelatex", "lualatex", "bibtex", "biber", "tlmgr"):
            if tool_status.get(tool, {}).get("available"):
                print(f"[OK]   {tool}: available")
    except Exception as e:  # never let the check itself crash startup
        print(f"[FAIL] LaTeX check failed: {e}")
        all_checks_passed = False

    # 4. Directories.
    from backend.config import ALLOW_SHELL_ESCAPE, FRONTEND_DIR, UPLOAD_DIR
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[OK]   Upload directory: {UPLOAD_DIR}")
    if FRONTEND_DIR.exists():
        print(f"[OK]   Frontend directory: {FRONTEND_DIR}")
    else:
        print(f"[FAIL] Frontend directory not found: {FRONTEND_DIR}")
        all_checks_passed = False

    # SECURITY: surfaced on every start, not just once at install time - an
    # opt-in made months ago is exactly the kind of thing a user forgets about.
    if ALLOW_SHELL_ESCAPE:
        print("[WARN] shell-escape is ENABLED — only compile documents you trust.")

    print("=" * 60)
    print("  All checks passed!" if all_checks_passed else "  Some checks FAILED. See messages above.")
    print("=" * 60)
    return all_checks_passed


def _configure_miktex_autoinstall(pdflatex_path: str) -> None:
    """Tell MiKTeX to install missing packages silently (the correct key is
    ``[MPM]AutoInstall``). No-op for non-MiKTeX distributions.

    Without this, a document that pulls in an uninstalled package makes MiKTeX
    ask for confirmation — a prompt that a headless subprocess can never answer,
    so the compile simply hangs until the timeout fires. Failure to apply the
    setting is only a warning: it degrades to "missing packages error out"
    rather than breaking anything that already worked.
    """
    if sys.platform != "win32" or "miktex" not in pdflatex_path.lower():
        return
    import subprocess
    try:
        subprocess.run(
            ["initexmf", "--set-config-value", "[MPM]AutoInstall=1"],
            # Quiet + bounded: this runs on every start, so a wedged initexmf
            # must not stall the launch or spam the console.
            capture_output=True, timeout=10,
        )
        print("[OK]   MiKTeX configured for silent auto-install")
    except Exception as e:
        print(f"[WARN] Could not configure MiKTeX auto-install: {e}")


# ─── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    """Parse the command line, run the pre-flight check, then serve the app.

    Flag defaults are pulled from ``backend.config`` so that an environment
    variable (``LATEX_HOST`` / ``LATEX_PORT``) configures the app while an
    explicit flag still overrides it.
    """
    from backend.config import HOST, PORT

    parser = argparse.ArgumentParser(description="LaTeX Studio - startup")
    parser.add_argument("--port", type=int, default=PORT, help=f"port (default: {PORT})")
    parser.add_argument("--host", type=str, default=HOST, help=f"host (default: {HOST})")
    parser.add_argument("--check", action="store_true", help="run pre-flight checks only")
    parser.add_argument("--reload", action="store_true", help="auto-reload (development)")
    parser.add_argument("--open-browser", action="store_true", help="open the browser on start")
    args = parser.parse_args()

    checks_passed = check_prerequisites()
    # --check is the install diagnostic documented in CONTRIBUTING.md: it
    # reports health through the exit code so a script never has to parse the
    # printed text, which is free to change.
    if args.check:
        sys.exit(0 if checks_passed else 1)
    if not checks_passed:
        print("\n[WARN] Starting anyway; some features may not work.\n")

    print(f"\n[START] LaTeX Studio on http://{args.host}:{args.port}")
    print(f"   API docs: http://{args.host}:{args.port}/docs")
    print("   Press Ctrl+C to stop.\n")

    if args.open_browser:
        import threading
        import webbrowser
        # Open after a short delay so uvicorn has time to bind the port.
        # It has to be a background timer because uvicorn.run() below never
        # returns - there is no "after the server started" point in this thread.
        threading.Timer(2.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()

    # Passed as an import string (not the app object) because uvicorn's
    # --reload needs to be able to re-import the module in a fresh process.
    import uvicorn
    uvicorn.run("backend.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
