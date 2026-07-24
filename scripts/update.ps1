# =============================================================================
#  update.ps1 - Bring everything up to date, safely and re-runnably.
#
#  Updates:
#    * Python packages     (uv pip install -U -r requirements.txt)
#    * TinyTeX packages     (tlmgr update --self --all)
#    * Monaco editor        (re-fetch the pinned version if missing/changed)
#  Does nothing destructive; safe to run any time.
# =============================================================================
$ErrorActionPreference = 'Continue'   # keep going even if one component fails
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not (Test-Path $VenvPython)) {
    Write-Err 'Not installed yet - run install.bat first.'
    Read-Host 'Press Enter to close'
    exit 1
}

$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot 'python'
$env:UV_CACHE_DIR          = Join-Path $ProjectRoot '.uvcache'

Write-Host ''
Write-Host '  ============================================================'
Write-Host '    LaTeX Studio - Updating'
Write-Host '  ============================================================'
Write-Host ''

# --- Python packages ---------------------------------------------------------
Write-Step '1/3  Updating Python packages'
$uv = Get-UvPath
if ($uv) {
    & $uv pip install --python $VenvPython -U -r (Join-Path $ProjectRoot 'requirements.txt')
    Write-Ok 'Python packages up to date'
} else {
    & $VenvPython -m pip install -U -r (Join-Path $ProjectRoot 'requirements.txt')
}

# --- TinyTeX packages --------------------------------------------------------
Write-Step '2/3  Updating TinyTeX packages'
$tlmgr = Join-Path $ProjectRoot 'tinytex\bin\windows\tlmgr.bat'
if (Test-Path $tlmgr) {
    & $tlmgr update --self --all
    Write-Ok 'TinyTeX packages up to date'
} else {
    Write-Warn 'TinyTeX not found in this folder; skipping (a system MiKTeX updates itself).'
}

# --- Monaco (re-fetch via the installer logic) -------------------------------
Write-Step '3/3  Checking the Monaco editor'
$monacoLoader = Join-Path $ProjectRoot 'frontend\vendor\monaco\vs\loader.js'
if (Test-Path $monacoLoader) {
    Write-Ok 'Monaco present (delete frontend\vendor\monaco and re-run install.bat to force a refresh)'
} else {
    Write-Warn 'Monaco missing; run install.bat to re-vendor it.'
}

Write-Host ''
Write-Ok 'Update complete.'
Write-Host ''
