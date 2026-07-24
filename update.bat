@echo off
REM ===========================================================================
REM  update.bat - update LaTeX Studio's dependencies to the latest versions.
REM
REM  Safe to run any time. Updates the Python packages, the TinyTeX packages
REM  and re-checks the bundled code editor. Thin wrapper over scripts\update.ps1.
REM ===========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1"
echo.
pause
