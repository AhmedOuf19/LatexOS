@echo off
REM ===========================================================================
REM  latex-pdf.bat - compile LaTeX to PDF from anywhere.
REM
REM    latex-pdf compile paper.tex
REM    latex-pdf compile thesis\ --json
REM    latex-pdf check
REM
REM  This is the entry point for callers outside the project - scripts, CI, and
REM  AI agents - so it is deliberately position-independent:
REM
REM  * PYTHONPATH (not "cd") makes the `backend` package importable. Changing
REM    the working directory would break every RELATIVE path the caller passed,
REM    which is the whole reason this wrapper exists.
REM  * PYTHONSAFEPATH stops Python prepending the CALLER'S directory to
REM    sys.path. Without it, running this from a folder that happens to contain
REM    a `backend` package imports THAT one instead of ours - silently using a
REM    different copy of the app, or crashing. A LaTeX project is an arbitrary
REM    folder, so this is a real collision, not a theoretical one.
REM  * %~dp0 resolves relative to this file, so the project can be installed
REM    anywhere, including a path containing spaces.
REM  * The interpreter is the project's own .venv, so the caller does not need
REM    Python on PATH and cannot accidentally use the wrong one.
REM ===========================================================================
setlocal
set "PYTHONPATH=%~dp0"
set "PYTHONSAFEPATH=1"
"%~dp0.venv\Scripts\python.exe" -m backend.cli %*
REM Propagate the CLI's exit code - callers branch on it (0 ok, 1 compile
REM failed, 2 usage, 3 no LaTeX distribution).
exit /b %ERRORLEVEL%
