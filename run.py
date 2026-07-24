"""
run.py – One-command startup for the LaTeX Studio application.

Usage:
    python run.py              → Start on http://localhost:8000
    python run.py --port 9000  → Start on a custom port
    python run.py --check      → Only check LaTeX installation, then exit
"""

import argparse
import sys
import os

# Ensure the project root is on the Python path
sys.path.insert(0, os.path.dirname(__file__))


def check_prerequisites():
    """Check that all required components are available."""
    print("=" * 60)
    print("  LaTeX Studio - Pre-flight Check")
    print("=" * 60)

    all_ok = True

    # 1. Python version
    py_ver = sys.version_info
    py_ok = py_ver >= (3, 10)
    status = "[OK]  " if py_ok else "[FAIL]"
    print(f"{status} Python {py_ver.major}.{py_ver.minor}.{py_ver.micro} {'(OK)' if py_ok else '(Need 3.10+)'}")
    if not py_ok:
        all_ok = False

    # 2. FastAPI / uvicorn
    try:
        import fastapi
        import uvicorn
        print(f"[OK]   FastAPI {fastapi.__version__}, uvicorn {uvicorn.__version__}")
    except ImportError as e:
        print(f"[FAIL] Missing dependency: {e}")
        print("  Run: pip install -r requirements.txt")
        all_ok = False

    # 3. LaTeX tools
    try:
        from backend.compiler import check_latex_available
        latex_status = check_latex_available()

        pdflatex_ok = latex_status.get("pdflatex", {}).get("available", False)
        latexmk_ok  = latex_status.get("latexmk",  {}).get("available", False)

        if pdflatex_ok:
            path = latex_status["pdflatex"]["path"]
            print(f"[OK]   pdflatex: {path}")
            
            # Configure MiKTeX to auto-install missing packages silently on Windows
            if sys.platform == "win32" and "miktex" in path.lower():
                import subprocess
                try:
                    subprocess.run(
                        ["initexmf", "--set-config-value", "[Core]AutoInstall=1"],
                        capture_output=True,
                        timeout=5
                    )
                    print("[OK]   MiKTeX configured for silent auto-install")
                except Exception as e:
                    print(f"[WARN] Failed to configure MiKTeX silent install: {e}")

        else:
            print("[FAIL] pdflatex NOT found")
            print("  Install MiKTeX: https://miktex.org/download")
            print("  Or TeX Live:    https://tug.org/texlive/")
            all_ok = False

        if latexmk_ok:
            print(f"[OK]   latexmk: {latex_status['latexmk']['path']}")
        else:
            print("  [WARN] latexmk not found (will use manual passes as fallback)")

        # Optional tools
        for tool in ("xelatex", "lualatex", "bibtex", "biber"):
            info = latex_status.get(tool, {})
            if info.get("available"):
                print(f"[OK]   {tool}: available")

    except Exception as e:
        print(f"[FAIL] LaTeX check failed: {e}")
        all_ok = False

    # 4. Upload directory
    from backend.config import UPLOAD_DIR, FRONTEND_DIR
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[OK]   Upload directory: {UPLOAD_DIR}")

    if FRONTEND_DIR.exists():
        print(f"[OK]   Frontend directory: {FRONTEND_DIR}")
    else:
        print(f"[FAIL] Frontend directory not found: {FRONTEND_DIR}")
        all_ok = False

    print("=" * 60)
    if all_ok:
        print("  All checks passed!")
    else:
        print("  Some checks FAILED. See messages above.")
    print("=" * 60)

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="LaTeX Studio – Startup")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--check", action="store_true", help="Only run pre-flight checks")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (development mode)")
    parser.add_argument("--open-browser", action="store_true", help="Automatically open the browser after starting")
    args = parser.parse_args()

    ok = check_prerequisites()

    if args.check:
        sys.exit(0 if ok else 1)

    if not ok:
        print("\n[WARN] Starting anyway, but some features may not work.\n")

    print(f"\n[START] Starting LaTeX Studio on http://{args.host}:{args.port}")
    print(f"   API docs: http://{args.host}:{args.port}/docs")
    print(f"   Press Ctrl+C to stop.\n")

    if args.open_browser:
        import threading
        import webbrowser
        def open_browser():
            print(f"\n[OK] Opening browser to http://{args.host}:{args.port} ...\n")
            webbrowser.open(f"http://{args.host}:{args.port}")
        
        # Start a background timer to open the browser after 2 seconds
        # This gives uvicorn time to bind to the port.
        threading.Timer(2.0, open_browser).start()

    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
