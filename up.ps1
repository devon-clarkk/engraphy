# Engraphy: one-command bring-up.
#
#   .\up.ps1
#
# Generates a .env with fresh random passwords if one does not exist, starts the
# stack, and waits until /healthz answers 200. Safe to re-run: compose is
# idempotent and an existing .env is never overwritten.
#
# Run .\provision.ps1 afterwards to create a space and mint a client token.

param(
    # Host port to publish the server on. Written into .env on first run.
    [int]$Port = 8000,
    # How long to wait for /healthz. The first run builds the images and
    # downloads the ~523 MB embedding model, so the default is generous.
    [int]$WaitSeconds = 1800
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function New-Secret {
    # Alphanumeric only, on purpose: these passwords are interpolated into a
    # postgres:// URL, and punctuation would need percent-encoding there.
    $chars = (48..57) + (65..90) + (97..122)
    -join ($chars | Get-Random -Count 32 | ForEach-Object { [char]$_ })
}

if (-not (Test-Path '.env')) {
    Write-Host '[up] no .env found; generating one with fresh random passwords'
    $lines = @(
        "POSTGRES_PASSWORD=$(New-Secret)",
        "ENGRAPHY_APP_ROLE_PASSWORD=$(New-Secret)",
        "ENGRAPHY_HOST_PORT=$Port"
    )
    Set-Content -Path '.env' -Value $lines -Encoding utf8
    Write-Host '[up] wrote .env (git-ignored; keep it, the database is tied to these passwords)'
} else {
    Write-Host '[up] using existing .env'
}

# Read the port back out of .env so the health probe targets the right one.
$port = '8000'
foreach ($line in (Get-Content '.env')) {
    if ($line -match '^\s*ENGRAPHY_HOST_PORT\s*=\s*(\S+)') { $port = $Matches[1] }
}

Write-Host '[up] starting stack (postgres -> init/migrate -> engraphy)...'

# Windows PowerShell 5.1 wraps every stderr line from a native command in an
# ErrorRecord, and `docker compose` writes its ordinary progress output to
# stderr. Under $ErrorActionPreference='Stop' that turns a successful bring-up
# into a NativeCommandError, so drop to 'Continue' here; $LASTEXITCODE below is
# the real success check.
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    docker compose up -d 2>&1 | ForEach-Object { Write-Host $_.ToString() }
} finally {
    $ErrorActionPreference = $prev
}

if ($LASTEXITCODE -ne 0) {
    Write-Host '[up] docker compose up failed (see the output above).'
    Write-Host "[up] if it says 'port is already allocated', set ENGRAPHY_HOST_PORT in .env to a free port and re-run."
    exit 1
}

$healthUrl = "http://127.0.0.1:$port/healthz"
Write-Host "[up] waiting up to ${WaitSeconds}s for $healthUrl"
Write-Host '[up] the first run builds the images and downloads the ~523 MB embedding model, so this is slow exactly once'

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$ok = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        # Not up yet. Keep waiting until the deadline.
    }
    Start-Sleep -Seconds 5
    Write-Host '  ...still waiting'
}

if (-not $ok) {
    Write-Host "[up] FAILED: $healthUrl did not return 200 within ${WaitSeconds}s."
    Write-Host '[up] check the logs with:  docker compose logs --tail 50 engraphy'
    exit 1
}

Write-Host "[up] healthz green at $healthUrl"
Write-Host "[up] MCP endpoint: http://127.0.0.1:$port/mcp/"
Write-Host '[up] next:  .\provision.ps1'
