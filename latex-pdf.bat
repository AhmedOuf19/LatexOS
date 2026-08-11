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
REM  * %~dp0 resolves relative to this file, so the project can be installed
REM    anywhere, including a path containing spaces.
REM  * The interpreter is the project's own .venv, so the caller does not need
REM    Python on PATH and cannot accidentally use the wrong one.
REM ===========================================================================
setlocal
set "PYTHONPATH=%~dp0"
"%~dp0.venv\Scripts\python.exe" -m backend.cli %*
REM Propagate the CLI's exit code - callers branch on it (0 ok, 1 compile
REM failed, 2 usage, 3 no LaTeX distribution).
exit /b %ERRORLEVEL%
