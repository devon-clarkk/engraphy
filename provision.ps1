# Engraphy: first-run provisioning.
#
#   .\provision.ps1                                             # space "default"
#   .\provision.ps1 -Space myspace -Principal alice -ClientName my-laptop
#
# Waits for /healthz, creates the space (plus its founding space_admin and that
# principal's personal scope), applies the starter pack, mints a scoped
# readwrite bearer token, and prints the exact client settings to paste in.
#
# The token is printed ONCE, here, and is never written to disk by this script.
# The server stores only its SHA-256, so it genuinely cannot be recovered later:
# copy it into your client before closing this window. If you lose it, re-run
# this script -- minting another token is cheap and harmless.
#
# Re-running is safe. An existing space or an already-applied pack is reported
# and skipped rather than treated as a failure, so a re-run still gets a fresh
# token.

param(
    [string]$Space = 'default',
    [string]$Principal = 'me',
    [string]$ClientName = 'my-client',
    # Ships inside the image, so no host path is needed and nothing here is tied
    # to where this checkout happens to live.
    [string]$Pack = '/app/packs/starter/pack.yaml',
    [int]$WaitSeconds = 600
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path '.env')) {
    Write-Host '[provision] no .env here. Run .\up.ps1 first.'
    exit 1
}

$port = '8000'
foreach ($line in (Get-Content '.env')) {
    if ($line -match '^\s*ENGRAPHY_HOST_PORT\s*=\s*(\S+)') { $port = $Matches[1] }
}

# --- 1. wait for the server ------------------------------------------------
# Polls /healthz directly rather than compose's health status: compose reports
# "starting" for up to start_period on first boot while the model cache volume
# is seeded, and that is indistinguishable from a crash-loop from the outside.
# A 200 from /healthz is the real signal.
$healthUrl = "http://127.0.0.1:$port/healthz"
Write-Host "[provision] waiting up to ${WaitSeconds}s for $healthUrl"

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$ok = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        # Not ready yet.
    }
    Start-Sleep -Seconds 5
    Write-Host '  ...still waiting'
}
if (-not $ok) {
    Write-Host "[provision] FAILED: $healthUrl did not return 200 within ${WaitSeconds}s."
    Write-Host '[provision] check the logs with:  docker compose logs --tail 50 engraphy'
    exit 1
}
Write-Host '[provision] healthz green'

# All three admin verbs run in the `admin` sidecar, whose ENGRAPHY_DATABASE_URL
# is already the superuser connection (see compose.yaml). That is why this
# script never reads or passes a password itself.
function Invoke-Admin {
    param([string[]]$AdminArgs)
    $composeArgs = @('compose', '--profile', 'admin', 'run', '--rm', '-T', 'admin', 'engraphy-admin') + $AdminArgs

    # Windows PowerShell 5.1 wraps every stderr line from a native command in an
    # ErrorRecord. `docker compose` writes its ordinary progress ("Container
    # engraphy-postgres-1  Running") to stderr, so under
    # $ErrorActionPreference='Stop' a perfectly successful call blows up as a
    # NativeCommandError. Drop to 'Continue' for the duration of the call and
    # flatten the result to plain strings; $LASTEXITCODE is still the truth.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & docker @composeArgs 2>&1
    } finally {
        $ErrorActionPreference = $prev
    }
    return @($raw | ForEach-Object { $_.ToString() })
}

# --- 2. create the space ---------------------------------------------------
Write-Host "[provision] creating space '$Space' (founding space_admin '$Principal')"
$out = Invoke-Admin @('space', 'create', '--id', $Space, '--display-name', $Space,
                      '--principal', $Principal, '--principal-display-name', $Principal)
if ($LASTEXITCODE -ne 0) {
    if ($out -match 'already exists|duplicate key') {
        Write-Host "  space '$Space' already exists; continuing"
    } else {
        Write-Host ($out -join "`n")
        Write-Host '[provision] FAILED at: space create'
        exit 1
    }
} else {
    Write-Host ($out -join "`n")
}

# --- 3. apply the pack -----------------------------------------------------
Write-Host "[provision] applying pack $Pack to '$Space'"
$out = Invoke-Admin @('pack', 'apply', $Pack, '--space', $Space)
if ($LASTEXITCODE -ne 0) {
    if ($out -match 'already applied|already exists') {
        Write-Host '  pack already applied; continuing'
    } else {
        Write-Host ($out -join "`n")
        Write-Host '[provision] FAILED at: pack apply'
        exit 1
    }
} else {
    Write-Host ($out -join "`n")
}

# --- 4. mint the token -----------------------------------------------------
Write-Host "[provision] minting a readwrite token for '$Principal'"
$out = Invoke-Admin @('token', 'create', '--space', $Space, '--principal', $Principal,
                      '--client-name', $ClientName, '--role', 'readwrite')
if ($LASTEXITCODE -ne 0) {
    Write-Host ($out -join "`n")
    Write-Host '[provision] FAILED at: token create'
    exit 1
}

# The CLI prints a "shown once" preamble line then the raw token on the next
# line. Anchor on that preamble rather than blindly taking the last line, so
# stray compose progress output cannot be mistaken for the token.
$lines = @($out | Where-Object { $_ -and ($_.Trim() -ne '') } | ForEach-Object { $_.Trim() })
$token = $null
for ($i = 0; $i -lt $lines.Count - 1; $i++) {
    if ($lines[$i] -match 'shown once') { $token = $lines[$i + 1]; break }
}
if (-not $token) { $token = $lines[-1] }

Write-Host ''
Write-Host '============================================================'
Write-Host ' ENGRAPHY TOKEN -- shown once, not stored anywhere on disk'
Write-Host '============================================================'
Write-Host $token
Write-Host '============================================================'
Write-Host ''
Write-Host 'In VS Code, run the command "Engraphy: Connect to a server" and paste:'
Write-Host "  server URL   http://127.0.0.1:$port/mcp/     (keep the trailing slash)"
Write-Host '  token        the token above'
Write-Host ''
Write-Host 'The extension stores the token in your OS keychain, not in settings.json.'
Write-Host "Optional label (Ctrl+, then search ""engraphy""):  engraphy.space  $Space"
Write-Host ''
Write-Host "Your personal scope in this space is: personal-$Principal"
