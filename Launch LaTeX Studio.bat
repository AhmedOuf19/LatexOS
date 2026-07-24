@echo off
REM ===========================================================================
REM  Launch LaTeX Studio.bat - start the app (fast, no downloads).
REM
REM  Double-click to run. If you have not installed yet, run install.bat first.
REM  Picks a free port automatically (it never kills other programs) and opens
REM  your browser. Close this window or press Ctrl+C to stop the server.
REM
REM  Thin wrapper over scripts\run.ps1.
REM ===========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
