# Resident memory of a native Windows Engraphy install.
#
#   ./scripts/footprint_windows.ps1 -InstallRoot "$env:LOCALAPPDATA\Programs\Engraphy"
#
# The Windows counterpart to scripts/footprint.py, which reads container
# cgroups. It is a DIFFERENT INSTRUMENT measuring the same quantity, and the two
# numbers should be compared with that in mind rather than placed in one column.
#
# WHAT IS COUNTED, AND WHY NOT WORKING SET
#
# `WorkingSet64`, which is what Task Manager's "Memory" column shows, includes
# shared pages, and PostgreSQL maps its shared buffer pool into every backend.
# Summing working sets across a postmaster and its backends therefore counts the
# buffer pool once per process: on a stock 128MB `shared_buffers` with six
# backends that is most of a gigabyte of memory that does not exist.
#
# So this sums WORKING SET PRIVATE across the processes, which excludes shared
# pages, and then adds the shared segment exactly once, read from the running
# server's own `shared_buffers` rather than assumed. That is the same shape as
# the cgroup reading `anon + shmem`: private plus shared, each counted once.
#
# Page cache is not counted, for the same reason footprint.py excludes `file`:
# it is reclaimable, and on Windows it belongs to the system rather than to any
# process.
#
# MEASURE AFTER A WORKLOAD, NEVER AT IDLE
#
# An idle Postgres has not faulted in its buffers and has not touched the HNSW
# index. It measured 29MB idle and 111MB after work in the container study
# (docs/footprint-2026-09-09.md), and the second is the number an operator lives
# with. Seed and exercise the store first:
#
#   psql -f scripts/footprint_workload.sql
#
# and drive real MCP traffic through the server before reading it here.

[CmdletBinding()]
param(
    # Only processes under this root are counted, so a PostgreSQL belonging to
    # some other product on the same machine cannot be swept into the total.
    [Parameter(Mandatory)][string]$InstallRoot,
    [string]$HealthzUrl,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$InstallRoot = (Resolve-Path $InstallRoot).Path.TrimEnd('\')

function Get-PrivateBytes {
    <#
      .SYNOPSIS
      Working Set Private for one process id.

      Read from the raw performance counter rather than from Get-Process,
      because .NET's Process object exposes WorkingSet64 and PrivateMemorySize64
      and neither is this: the first counts shared pages, the second counts
      committed private bytes including what has been paged out.
    #>
    param([int]$ProcessId)
    $c = Get-CimInstance Win32_PerfRawData_PerfProc_Process -Filter "IDProcess=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $c) { return 0 }
    return [int64]$c.WorkingSetPrivate
}

function Get-Group {
    param([string]$Name, [string]$Pattern)
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($InstallRoot, [System.StringComparison]::OrdinalIgnoreCase) `
            -and $_.Name -like $Pattern
    }
    $rows = foreach ($p in $procs) {
        [pscustomobject]@{ Pid = $p.Id; Name = $p.Name; PrivateBytes = (Get-PrivateBytes $p.Id) }
    }
    [pscustomobject]@{
        Group = $Name
        Count = @($rows).Count
        PrivateBytes = (@($rows) | Measure-Object -Property PrivateBytes -Sum).Sum
        Processes = @($rows)
    }
}

$postgres = Get-Group -Name 'postgres' -Pattern 'postgres'
$server = Get-Group -Name 'engraphy' -Pattern 'engraphy-win'

if ($postgres.Count -eq 0 -and $server.Count -eq 0) {
    throw "nothing from $InstallRoot is running. Start it first: engraphy-win.exe run"
}

# The shared segment, counted once. Read from the server rather than assumed,
# because the whole point of compose.small.yaml and the equivalent tuning in
# engraphy_win._tune_postgresql_conf is that this value is not the default.
$sharedBytes = 0
$sharedSource = 'not read (postgres is not running)'
if ($postgres.Count -gt 0) {
    $psql = Join-Path $InstallRoot 'pgsql/bin/psql.exe'
    $cfgPath = Join-Path $env:LOCALAPPDATA 'Engraphy/engraphy.json'
    if ((Test-Path $psql) -and (Test-Path $cfgPath)) {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        $env:PGPASSWORD = $cfg.superuser_password
        $blocks = & $psql -tAq -h 127.0.0.1 -p $cfg.pg_port -U postgres -d engraphy `
            -c "SELECT setting::bigint * (SELECT setting::bigint FROM pg_settings WHERE name='block_size') FROM pg_settings WHERE name='shared_buffers'" 2>$null
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -eq 0 -and $blocks) {
            $sharedBytes = [int64]($blocks.Trim())
            $sharedSource = 'pg_settings.shared_buffers'
        } else {
            $sharedSource = 'could not be read from the running server'
        }
    } else {
        $sharedSource = 'psql or engraphy.json not found, so it was not read'
    }
}

$total = $postgres.PrivateBytes + $server.PrivateBytes + $sharedBytes

$result = [pscustomobject]@{
    measured_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
    install_root = $InstallRoot
    postgres_processes = $postgres.Count
    postgres_private_mb = [math]::Round($postgres.PrivateBytes / 1MB, 1)
    shared_buffers_mb = [math]::Round($sharedBytes / 1MB, 1)
    shared_buffers_source = $sharedSource
    server_processes = $server.Count
    server_private_mb = [math]::Round($server.PrivateBytes / 1MB, 1)
    total_mb = [math]::Round($total / 1MB, 1)
}

if ($HealthzUrl) {
    try {
        $result | Add-Member healthz (Invoke-RestMethod -Uri $HealthzUrl -TimeoutSec 5)
    } catch {
        $result | Add-Member healthz "unreachable: $($_.Exception.Message)"
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 5
    return
}

Write-Output ''
Write-Output "Engraphy resident memory, native Windows, $($result.measured_at)"
Write-Output "  install root: $InstallRoot"
Write-Output ''
Write-Output ("  {0,-34}{1,10}" -f 'component', 'MB')
Write-Output ("  {0,-34}{1,10}" -f ('postgres, ' + $postgres.Count + ' processes (private)'), $result.postgres_private_mb)
Write-Output ("  {0,-34}{1,10}" -f 'shared buffer pool (once)', $result.shared_buffers_mb)
Write-Output ("  {0,-34}{1,10}" -f ('engraphy, ' + $server.Count + ' process (private)'), $result.server_private_mb)
Write-Output ("  {0,-34}{1,10}" -f 'TOTAL', $result.total_mb)
Write-Output ''
Write-Output "  shared buffer pool read from: $sharedSource"
Write-Output '  Working Set Private per process, plus the shared segment counted once.'
Write-Output '  Not the same instrument as the cgroup figures in docs/footprint-2026-09-09.md.'
foreach ($g in @($postgres, $server)) {
    foreach ($p in $g.Processes) {
        Write-Output ("    {0,-12} pid {1,-8} {2,8:N1} MB" -f $p.Name, $p.Pid, ($p.PrivateBytes / 1MB))
    }
}
