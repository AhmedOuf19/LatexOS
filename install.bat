@echo off
REM ===========================================================================
REM  install.bat - one-time setup for LaTeX Studio.
REM
REM  Double-click this ONCE. It installs Python, LaTeX (TinyTeX), the code
REM  editor and all dependencies INTO this folder - nothing system-wide, no
REM  admin rights. After it finishes, use "Launch LaTeX Studio.bat" to run.
REM
REM  This is a thin wrapper; the real work is in scripts\install.ps1, which
REM  gives proper HTTPS, checksum checks, progress and error handling.
REM ===========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
echo.
pause
