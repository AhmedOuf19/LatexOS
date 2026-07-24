# =============================================================================
#  install.ps1 - First-time, self-contained setup for LaTeX Studio.
#
#  Installs EVERYTHING into the project folder (nothing system-wide, no admin,
#  no PATH/registry changes). Deleting the folder removes the whole install.
#
#  Layers:
#    1. uv            -> bin\uv.exe          (portable Python & package manager)
#    2. Python + venv -> python\ , .venv\    (via uv, inside the folder)
#    3. Python deps   -> .venv\              (from requirements.txt)
#    4. TinyTeX       -> tinytex\            (portable LaTeX; falls back to a
#                                             system MiKTeX/TeX Live if present)
#    5. Monaco editor -> frontend\vendor\monaco\
#    6. Fonts         -> frontend\vendor\fonts\   (committed, but re-fetched here
#                                                  if missing)
#
#  Safe to re-run: each step is skipped when already satisfied. Progress is
#  logged to logs\install.log. Run it by double-clicking install.bat.
# =============================================================================
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

# Pinned versions. Update these (and re-run) to move to newer releases.
$UV_URL      = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
$PYTHON_VER  = '3.12'
$MONACO_VER  = '0.52.2'
$MONACO_URL  = "https://registry.npmjs.org/monaco-editor/-/monaco-editor-$MONACO_VER.tgz"
$TINYTEX_API = 'https://api.github.com/repos/rstudio/tinytex-releases/releases/latest'
$FONT_INTER  = 'https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5/files/inter-latin-wght-normal.woff2'
$FONT_MONO   = 'https://cdn.jsdelivr.net/npm/@fontsource-variable/jetbrains-mono@5/files/jetbrains-mono-latin-wght-normal.woff2'

# Keep uv's downloaded Pythons and cache inside the folder (portability).
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot 'python'
$env:UV_CACHE_DIR          = Join-Path $ProjectRoot '.uvcache'

$manifest = @{}
Read-Manifest | Get-Member -MemberType NoteProperty | ForEach-Object {
    $manifest[$_.Name] = (Read-Manifest).($_.Name)
}

Write-Host ''
Write-Host '  ============================================================'
Write-Host '    LaTeX Studio - Installing (everything into this folder)'
Write-Host '  ============================================================'
Write-Host ''

# --- 1. uv -------------------------------------------------------------------
Write-Step '1/6  uv (portable Python manager)'
$uv = Get-UvPath
if (-not $uv) {
    $zip = Join-Path $env:TEMP 'uv.zip'
    if (Get-File $UV_URL $zip) {
        Expand-Archive -Path $zip -DestinationPath (Join-Path $ProjectRoot 'bin') -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
        $uv = Get-UvPath
    }
}
if (-not $uv) { Write-Err 'Could not obtain uv. Aborting.'; exit 1 }
Write-Ok "uv ready: $uv"
$manifest['uv'] = (& $uv --version) 2>$null

# --- 2. Python + virtual environment ----------------------------------------
Write-Step "2/6  Python $PYTHON_VER + virtual environment (.venv)"
if (-not (Test-Path $VenvPython)) {
    & $uv python install $PYTHON_VER
    & $uv venv (Join-Path $ProjectRoot '.venv') --python $PYTHON_VER
}
if (-not (Test-Path $VenvPython)) { Write-Err 'Virtual environment was not created. Aborting.'; exit 1 }
Write-Ok 'Python & .venv ready'
$manifest['python'] = (& $VenvPython -c "import platform;print(platform.python_version())") 2>$null

# --- 3. Python dependencies --------------------------------------------------
Write-Step '3/6  Python packages (requirements.txt)'
& $uv pip install --python $VenvPython -r (Join-Path $ProjectRoot 'requirements.txt')
Write-Ok 'Python packages installed'
$manifest['requirements_hash'] = (Get-FileHash (Join-Path $ProjectRoot 'requirements.txt') -Algorithm SHA256).Hash

# --- 4. TinyTeX (portable LaTeX) --------------------------------------------
Write-Step '4/6  TinyTeX (portable LaTeX distribution)'
$tinytexBin = Join-Path $ProjectRoot 'tinytex\bin\windows\pdflatex.exe'
if (Test-Path $tinytexBin) {
    Write-Ok 'TinyTeX already present'
} else {
    try {
        $rel = Invoke-RestMethod -Uri $TINYTEX_API -UseBasicParsing
        # TinyTeX-1 = installer with ~100 common packages (good default).
        $asset = $rel.assets | Where-Object { $_.name -match '^TinyTeX-1.*\.zip$' } | Select-Object -First 1
        if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match '^TinyTeX.*\.zip$' } | Select-Object -First 1 }
        if ($asset) {
            $zip = Join-Path $env:TEMP $asset.name
            if (Get-File $asset.browser_download_url $zip) {
                # Extract to a SEPARATE temp dir first. Doing this avoids a
                # case-insensitive-filesystem trap on Windows: the archive's
                # top folder is 'TinyTeX', and a 'tinytex' guard would match and
                # delete it. Using Windows' own tar.exe (bsdtar) is also far
                # faster than Expand-Archive and handles C:\ paths correctly.
                $tmp = Join-Path $env:TEMP 'tinytex-extract'
                if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
                New-Item -ItemType Directory -Path $tmp -Force | Out-Null
                & "$env:SystemRoot\System32\tar.exe" -xf $zip -C $tmp
                # Find the extracted distribution folder (name varies across
                # releases: TinyTeX / .TinyTeX). Fall back to the temp dir itself
                # if the archive had no single top-level folder.
                $inner = Get-ChildItem $tmp -Directory |
                    Where-Object { Test-Path (Join-Path $_.FullName 'bin') } |
                    Select-Object -First 1
                $srcDir = if ($inner) { $inner.FullName } else { $tmp }
                $dst = Join-Path $ProjectRoot 'tinytex'
                if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
                Move-Item $srcDir $dst
                Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        Write-Warn "TinyTeX download failed: $($_.Exception.Message)"
    }
    if (Test-Path $tinytexBin) {
        Write-Ok 'TinyTeX installed'
        # Ask tlmgr to install missing packages on demand.
        & (Join-Path $ProjectRoot 'tinytex\bin\windows\tlmgr.bat') option autobackup 0 2>$null | Out-Null
    } else {
        Write-Warn 'TinyTeX not installed. The app will look for a system MiKTeX/TeX Live instead.'
        Write-Warn 'If you have neither, install one from https://miktex.org or https://yihui.org/tinytex/'
    }
}
$manifest['tinytex'] = (Test-Path $tinytexBin)

# --- 5. Monaco editor (offline) ---------------------------------------------
Write-Step '5/6  Monaco editor (in-browser code editor)'
$monacoLoader = Join-Path $ProjectRoot 'frontend\vendor\monaco\vs\loader.js'
if (Test-Path $monacoLoader) {
    Write-Ok 'Monaco already vendored'
} else {
    $tgz = Join-Path $env:TEMP 'monaco.tgz'
    if (Get-File $MONACO_URL $tgz) {
        $tmp = Join-Path $env:TEMP 'monaco-extract'
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        # Use Windows' own tar.exe (bsdtar, in System32) explicitly. Calling a
        # bare `tar` can resolve to Git's GNU tar, which misreads a C:\ path as a
        # remote host ("Cannot connect to C:"). bsdtar auto-detects the gzip.
        & "$env:SystemRoot\System32\tar.exe" -xf $tgz -C $tmp
        $src = Join-Path $tmp 'package\min\vs'
        $dst = Join-Path $ProjectRoot 'frontend\vendor\monaco\vs'
        if (Test-Path $src) {
            if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
            New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
            Copy-Item $src $dst -Recurse -Force
            Write-Ok 'Monaco vendored'
        } else {
            Write-Warn 'Monaco archive layout unexpected; the app will use a plain-textarea editor.'
        }
        Remove-Item $tgz, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$manifest['monaco'] = (Test-Path $monacoLoader)

# --- 6. Fonts (small; usually already committed) ----------------------------
Write-Step '6/6  Fonts (offline UI rendering)'
$interDst = Join-Path $ProjectRoot 'frontend\vendor\fonts\inter.woff2'
$monoDst  = Join-Path $ProjectRoot 'frontend\vendor\fonts\jetbrains-mono.woff2'
if (-not (Test-Path $interDst)) { Get-File $FONT_INTER $interDst | Out-Null }
if (-not (Test-Path $monoDst))  { Get-File $FONT_MONO  $monoDst  | Out-Null }
Write-Ok 'Fonts ready (missing fonts fall back to system fonts automatically)'
$manifest['fonts'] = ((Test-Path $interDst) -and (Test-Path $monoDst))

# --- Finish ------------------------------------------------------------------
$manifest['app_version'] = (Get-Content (Join-Path $ProjectRoot 'backend\__init__.py') |
    Select-String '__version__' | ForEach-Object { $_.Line.Split('"')[1] })
Save-Manifest $manifest

Write-Host ''
Write-Ok 'Installation complete.'
Write-Host ''
Write-Host '  Next: double-click "Launch LaTeX Studio.bat" to start the app.'
Write-Host ''
