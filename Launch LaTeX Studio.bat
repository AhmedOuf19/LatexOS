@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title LaTeX Studio - Setup ^& Launch

REM ===========================================================================
REM   LaTeX Studio — Portable Launcher
REM   Double-click this file to set up and run the application.
REM   Everything is automatic. No prior installs required.
REM ===========================================================================

REM -- Switch to the directory where this .bat file lives --
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

echo.
echo  ============================================================
echo    LaTeX Studio - Automatic Setup
echo  ============================================================
echo.

REM ===========================================================================
REM   STEP 1: Find or Install Python
REM ===========================================================================

set "PYTHON="

REM -- 1a. Check common NATIVE Windows install locations FIRST --
REM    (These always produce correct Scripts\ venvs, unlike MSYS/Cygwin)
for %%V in (313 312 311 310) do (
    if not defined PYTHON (
        REM Per-user installs (most common)
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
            if !errorlevel! equ 0 set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        )
        REM System-wide installs
        if exist "C:\Python%%V\python.exe" (
            if !errorlevel! equ 0 set "PYTHON=C:\Python%%V\python.exe"
        )
        if exist "C:\Program Files\Python%%V\python.exe" (
            if !errorlevel! equ 0 set "PYTHON=C:\Program Files\Python%%V\python.exe"
        )
    )
)

REM -- 1b. Check py launcher (official Windows Python Launcher) --
if not defined PYTHON (
    where py >nul 2>&1
    if !errorlevel! equ 0 (
        for /f "tokens=*" %%p in ('py -3 -c "import sys; print(sys.executable)"') do set "PYTHON=%%p"
    )
)

REM -- 1c. Check PATH (skip MSYS/Cygwin/MinGW — they create incompatible venvs) --
if not defined PYTHON (
    where python >nul 2>&1
    if !errorlevel! equ 0 (
        for /f "tokens=*" %%p in ('where python 2^>nul') do (
            if not defined PYTHON (
                echo %%p | findstr /i /c:"msys" /c:"cygwin" /c:"mingw" >nul 2>&1
                if !errorlevel! neq 0 (
                    set "PYTHON=%%p"
                )
            )
        )
    )
)

REM -- 1d. If no Python found, download and install it --
if not defined PYTHON (
    echo  [!] Python 3.10+ not found on this computer.
    echo  [*] Downloading Python 3.11.9 installer... please wait.
    echo.

    set "PY_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"
    set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

    REM Try PowerShell download
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('%PY_URL%', '%PY_INSTALLER%') } catch { exit 1 }" >nul 2>&1
    if !errorlevel! neq 0 (
        REM Try curl as fallback
        curl -L -o "!PY_INSTALLER!" "%PY_URL%" >nul 2>&1
    )

    if not exist "!PY_INSTALLER!" (
        echo  [ERROR] Failed to download Python installer.
        echo          Please install Python 3.10+ manually from https://python.org
        echo          Then re-run this script.
        pause
        exit /b 1
    )

    echo  [*] Installing Python 3.11.9 [per-user, no admin required]...
    echo      This may take 1-2 minutes.
    "!PY_INSTALLER!" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
    if !errorlevel! neq 0 (
        echo  [ERROR] Python installation failed.
        echo          Please install Python 3.10+ manually from https://python.org
        pause
        exit /b 1
    )

    REM Refresh PATH so we can find the newly installed Python
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"

    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) else (
        echo  [ERROR] Python was installed but cannot be found.
        echo          Please close this window, open a NEW command prompt, and try again.
        pause
        exit /b 1
    )

    echo  [OK] Python installed successfully.
    echo.
    del "!PY_INSTALLER!" >nul 2>&1
)

REM -- Display found Python version --
for /f "tokens=*" %%v in ('"%PYTHON%" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"') do set "PY_VER=%%v"
echo  [OK] Python %PY_VER% found at: %PYTHON%

REM ===========================================================================
REM   STEP 2: Create Virtual Environment
REM ===========================================================================

set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

REM Fallback: some Python builds (MSYS2) use bin\ instead of Scripts\
if not exist "%VENV_PYTHON%" (
    if exist "%VENV_DIR%\bin\python.exe" (
        set "VENV_PYTHON=%VENV_DIR%\bin\python.exe"
    )
)

if not exist "%VENV_PYTHON%" (
    echo  [*] Creating virtual environment [.venv]...
    "%PYTHON%" -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    REM Re-detect the correct python path inside the venv
    if exist "%VENV_DIR%\Scripts\python.exe" (
        set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
    ) else if exist "%VENV_DIR%\bin\python.exe" (
        set "VENV_PYTHON=%VENV_DIR%\bin\python.exe"
    ) else (
        echo  [ERROR] Virtual environment created but python.exe not found inside it.
        echo          Try deleting the .venv folder and re-running this script.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
) else (
    echo  [OK] Virtual environment exists.
)

REM ===========================================================================
REM   STEP 3: Install Python Packages
REM ===========================================================================

"%VENV_PYTHON%" -c "import fastapi" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [*] Installing Python packages [first time only]...
    "%VENV_PYTHON%" -m pip install --upgrade pip --quiet >nul 2>&1
    "%VENV_PYTHON%" -m pip install -r "%SCRIPT_DIR%\requirements.txt" --quiet
    if !errorlevel! neq 0 (
        echo  [ERROR] Failed to install Python packages.
        echo          Check your internet connection and try again.
        pause
        exit /b 1
    )
    echo  [OK] All Python packages installed.
) else (
    echo  [OK] Python packages already installed.
)

REM ===========================================================================
REM   STEP 4: Find or Install MiKTeX (LaTeX Distribution)
REM ===========================================================================

set "LATEX_FOUND="

REM -- 4a. Check PATH for pdflatex --
where pdflatex >nul 2>&1
if %errorlevel% equ 0 (
    set "LATEX_FOUND=1"
    for /f "tokens=*" %%p in ('where pdflatex') do (
        echo  [OK] pdflatex found: %%p
        goto :latex_done
    )
)

REM -- 4b. Check common MiKTeX locations --
for %%D in (
    "%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64"
    "C:\Program Files\MiKTeX\miktex\bin\x64"
    "C:\Program Files (x86)\MiKTeX\miktex\bin"
    "%LOCALAPPDATA%\Programs\MiKTeX 2.9\miktex\bin\x64"
) do (
    if exist "%%~D\pdflatex.exe" (
        set "LATEX_FOUND=1"
        echo  [OK] pdflatex found: %%~D\pdflatex.exe
        REM Add to PATH for this session
        set "PATH=%%~D;!PATH!"
        goto :latex_done
    )
)

REM -- 4c. Check common TeX Live locations --
for %%Y in (2026 2025 2024 2023 2022) do (
    if exist "C:\texlive\%%Y\bin\windows\pdflatex.exe" (
        set "LATEX_FOUND=1"
        echo  [OK] pdflatex found: C:\texlive\%%Y\bin\windows\pdflatex.exe
        set "PATH=C:\texlive\%%Y\bin\windows;!PATH!"
        goto :latex_done
    )
)

REM -- 4d. If no LaTeX found, download and install MiKTeX --
if not defined LATEX_FOUND (
    echo.
    echo  [!] No LaTeX distribution found (pdflatex not available).
    echo  [*] Downloading MiKTeX installer [~250 MB]... please wait.
    echo      This is required to compile LaTeX documents.
    echo.

    set "MIKTEX_INSTALLER=%TEMP%\miktexsetup.exe"
    set "MIKTEX_URL=https://miktex.org/download/ctan/systems/win32/miktex/setup/windows-x64/basic-miktex-24.1-x64.exe"

    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('%MIKTEX_URL%', '%MIKTEX_INSTALLER%') } catch { exit 1 }" >nul 2>&1
    if !errorlevel! neq 0 (
        curl -L -o "!MIKTEX_INSTALLER!" "%MIKTEX_URL%" >nul 2>&1
    )

    if not exist "!MIKTEX_INSTALLER!" (
        echo  [ERROR] Failed to download MiKTeX installer.
        echo          Please install MiKTeX manually from https://miktex.org/download
        echo          Then re-run this script.
        pause
        exit /b 1
    )

    echo  [*] Installing MiKTeX [per-user, no admin required]...
    echo      This may take 3-5 minutes. Please be patient.
    "!MIKTEX_INSTALLER!" --unattended --user-install --auto-install=yes
    if !errorlevel! neq 0 (
        echo  [WARN] MiKTeX installer returned a non-zero exit code.
        echo         Checking if installation succeeded anyway...
    )

    REM Refresh PATH to include MiKTeX
    set "PATH=%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64;!PATH!"

    if exist "%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe" (
        set "LATEX_FOUND=1"
        echo  [OK] MiKTeX installed successfully.
    ) else (
        echo  [ERROR] MiKTeX installation could not be verified.
        echo          Please install MiKTeX manually from https://miktex.org/download
        echo          Then re-run this script.
        pause
        exit /b 1
    )

    del "!MIKTEX_INSTALLER!" >nul 2>&1
)

:latex_done

REM ===========================================================================
REM   STEP 5: Configure MiKTeX for Silent Auto-Install of Packages
REM ===========================================================================

where initexmf >nul 2>&1
if %errorlevel% equ 0 (
    initexmf --set-config-value "[MPM]AutoInstall=1" >nul 2>&1
    echo  [OK] MiKTeX configured: auto-install missing packages = ON
)

REM ===========================================================================
REM   STEP 6: Free Port 8000 If In Use
REM ===========================================================================

for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do (
    echo  [*] Freeing port 8000 [PID %%a]...
    taskkill /PID %%a /F >nul 2>&1
)

REM ===========================================================================
REM   STEP 7: Run Pre-Flight Check & Launch
REM ===========================================================================

echo.
echo  ============================================================
echo    All checks passed! Starting LaTeX Studio...
echo  ============================================================
echo.
echo    App:      http://localhost:8000
echo    API docs: http://localhost:8000/docs
echo    Close this window to stop the server.
echo.
echo  ============================================================
echo.

"%VENV_PYTHON%" run.py --port 8000 --open-browser

echo.
echo  Server stopped.
pause
