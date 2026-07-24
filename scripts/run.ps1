# =============================================================================
#  run.ps1 - Start LaTeX Studio (fast path, NO network access).
#
#  Verifies the in-folder install exists, picks a free port (it does NOT kill
#  whatever owns port 8000), starts the server on 127.0.0.1 and opens the
#  browser. Close the window or press Ctrl+C to stop.
# =============================================================================
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

# --- Verify the install is present -------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Write-Err 'This app is not installed yet.'
    Write-Host ''
    Write-Host '  Please run  install.bat  first (one-time setup).'
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit 1
}

# Point the backend at the folder-local TinyTeX if it exists.
$tinytexBinDir = Join-Path $ProjectRoot 'tinytex\bin\windows'
if (Test-Path (Join-Path $tinytexBinDir 'pdflatex.exe')) {
    $env:LATEX_BIN_PATH = $tinytexBinDir
}

# --- Pick a free port --------------------------------------------------------
$port = Get-FreePort -Preferred 8000
if ($port -ne 8000) {
    Write-Warn "Port 8000 is in use; starting on port $port instead."
}

Write-Host ''
Write-Host '  ============================================================'
Write-Host '    LaTeX Studio'
Write-Host '  ============================================================'
Write-Host "    App:      http://127.0.0.1:$port"
Write-Host "    API docs: http://127.0.0.1:$port/docs"
Write-Host '    Close this window or press Ctrl+C to stop the server.'
Write-Host '  ============================================================'
Write-Host ''

# --- Launch (blocks until the server exits) ----------------------------------
& $VenvPython (Join-Path $ProjectRoot 'run.py') --host 127.0.0.1 --port $port --open-browser
$code = $LASTEXITCODE

Write-Host ''
Write-Host '  Server stopped.'
exit $code
