# =============================================================================
#  common.ps1 - shared helpers for the LaTeX Studio launcher scripts.
#
#  Dot-sourced by install.ps1 / run.ps1 / update.ps1. Not meant to be run alone.
#  Targets Windows PowerShell 5.1 (the version shipped with Windows), so it
#  avoids the ternary / null-coalescing operators that only exist in PS 7+.
# =============================================================================

# Force TLS 1.2 for every web request (older Windows defaults can break HTTPS).
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- Paths -------------------------------------------------------------------
# $ProjectRoot is the folder that contains this script's parent (the repo root).
$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:LogDir       = Join-Path $ProjectRoot 'logs'
$script:ManifestPath = Join-Path $ProjectRoot '.install-state.json'
$script:VenvPython   = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# --- Logging -----------------------------------------------------------------
function Write-Log {
    param([string]$Message, [string]$Level = 'INFO', [string]$LogFile = 'install.log')
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line  = "[$stamp] [$Level] $Message"
    $color = @{ INFO = 'Gray'; OK = 'Green'; WARN = 'Yellow'; ERROR = 'Red'; STEP = 'Cyan' }[$Level]
    if (-not $color) { $color = 'Gray' }
    Write-Host $line -ForegroundColor $color
    Add-Content -Path (Join-Path $LogDir $LogFile) -Value $line -Encoding utf8
}

function Write-Step { param([string]$Message) Write-Log $Message 'STEP' }
function Write-Ok   { param([string]$Message) Write-Log $Message 'OK' }
function Write-Warn { param([string]$Message) Write-Log $Message 'WARN' }
function Write-Err  { param([string]$Message) Write-Log $Message 'ERROR' }

# --- Downloads ---------------------------------------------------------------
function Get-File {
    <#
      Download $Url to $Dest. If $Sha256 is supplied, the file is rejected
      unless its hash matches (integrity / anti-tamper). Returns $true on
      success, $false on any failure (never throws).
    #>
    param([string]$Url, [string]$Dest, [string]$Sha256 = '')
    try {
        $dir = Split-Path -Parent $Dest
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Write-Log "Downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
    } catch {
        Write-Err "Download failed: $Url  ($($_.Exception.Message))"
        return $false
    }
    if ($Sha256) {
        $actual = (Get-FileHash -Path $Dest -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $Sha256.ToLower()) {
            Write-Err "Checksum mismatch for $Dest (expected $Sha256, got $actual). Deleting."
            Remove-Item $Dest -Force -ErrorAction SilentlyContinue
            return $false
        }
        Write-Ok "Checksum verified for $(Split-Path -Leaf $Dest)"
    }
    return $true
}

# --- Manifest (records what is installed, with versions) ---------------------
function Read-Manifest {
    if (Test-Path $ManifestPath) {
        try { return Get-Content $ManifestPath -Raw | ConvertFrom-Json } catch { }
    }
    return [pscustomobject]@{}
}

function Save-Manifest {
    param([hashtable]$Data)
    $Data['updated'] = (Get-Date).ToString('o')
    ($Data | ConvertTo-Json -Depth 5) | Set-Content -Path $ManifestPath -Encoding utf8
}

# --- Networking --------------------------------------------------------------
function Get-FreePort {
    <# Return $Preferred if it is free, otherwise the next free port above it. #>
    param([int]$Preferred = 8000)
    for ($p = $Preferred; $p -lt $Preferred + 50; $p++) {
        $listener = $null
        try {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)
            $listener.Start()
            return $p
        } catch {
            continue
        } finally {
            if ($listener) { $listener.Stop() }
        }
    }
    return $Preferred
}

# --- Tool discovery ----------------------------------------------------------
function Get-UvPath {
    $local = Join-Path $ProjectRoot 'bin\uv.exe'
    if (Test-Path $local) { return $local }
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}
