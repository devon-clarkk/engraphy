# Assemble everything the Windows installer lays down, into one directory.
#
#   ./deploy/windows/build-payload.ps1 `
#       -PgvectorDir artifacts -ServerDir artifacts/server -ModelDir artifacts/model `
#       -Out build/windows/payload
#
# Called by .github/workflows/windows-dist.yml, and runnable by hand against
# artifacts downloaded from a CI run, which is how the footprint measurements in
# docs/windows-native.md were taken.
#
# The result is exactly the install directory: engraphy-win.exe at the top with
# pgsql\ beside it, because engraphy_win.install_root() is the directory holding
# the executable. Nothing is rearranged at install time, so what is measured here
# is what the user ends up with.
#
# THE TRIM IS THE INTERESTING PART
#
# EDB's Windows archive is 883MB unpacked, and 722MB of that is pgAdmin, a
# desktop database GUI that has nothing to do with running a server. What remains
# after the trim below is 66MB, and every removal is a category rather than a
# hand-picked file, so a new patch release does not quietly reintroduce
# something:
#
#   pgAdmin 4, StackBuilder   applications, not the server
#   doc                       19MB of HTML
#   include                   headers, for building extensions. pgvector is
#                             already built (see pgvector-windows.yml), and
#                             nothing downstream compiles against this tree
#   lib\*.lib                 link libraries, for the same reason
#   share\locale              22MB of message translations
#   bin\*.exe not on the keep list, and bin\wx*.dll
#                             the client and maintenance tools nothing here runs
#
# The keep list is explicit rather than a deny list, because the failure mode of
# guessing wrong is a tool missing at the moment an operator needs it most.

[CmdletBinding()]
param(
    [string]$PgUrl = 'https://get.enterprisedb.com/postgresql/postgresql-16.15-3-windows-x64-binaries.zip',
    [string]$PgSha256 = '5e8afffe67daf949aeeb03b74951f1ec2324e1888f73fbd036ab0e567ab004d9',
    [string]$PgZip,
    [Parameter(Mandatory)][string]$PgvectorDir,
    [Parameter(Mandatory)][string]$ServerDir,
    [Parameter(Mandatory)][string]$ModelDir,
    [Parameter(Mandatory)][string]$Out
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $here '..' '..')).Path

# Every executable Engraphy runs, and the ones an operator needs when something
# has gone wrong. pg_dump and pg_restore are not optional: `engraphy-admin
# migrate` takes an unconditional pre-migration dump through pg_dump, which is
# the property that makes an upgrade recoverable.
$KEEP_EXE = @(
    'postgres.exe',        # the server
    'initdb.exe',          # first run
    'pg_ctl.exe',          # start and stop
    'psql.exe',            # provision-app-role.sql, and operator use
    'pg_isready.exe',      # readiness, before the server is allowed to connect
    'pg_dump.exe',         # migrate's unconditional pre-dump
    'pg_restore.exe',      # and restoring one
    'createdb.exe',
    'pg_config.exe',       # version reporting
    'pg_controldata.exe',  # diagnosing a cluster that will not start
    'pg_resetwal.exe'      # last-resort recovery
)

function Get-Postgres {
    param([string]$Destination)

    if ($PgZip) {
        $zip = (Resolve-Path $PgZip).Path
    } else {
        $zip = Join-Path ([System.IO.Path]::GetTempPath()) 'engraphy-pg-binaries.zip'
        if (-not (Test-Path $zip)) {
            Write-Output "downloading $PgUrl"
            Invoke-WebRequest -Uri $PgUrl -OutFile $zip
        }
    }
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $PgSha256.ToLower()) {
        throw "PostgreSQL archive sha256 is $got, expected $PgSha256"
    }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("engraphy-pg-" + [System.Guid]::NewGuid().ToString('N'))
    Expand-Archive $zip -DestinationPath $tmp
    Move-Item (Join-Path $tmp 'pgsql') $Destination
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

function Remove-Unshipped {
    param([string]$PgRoot)

    foreach ($d in @('pgAdmin 4', 'StackBuilder', 'doc', 'include')) {
        $p = Join-Path $PgRoot $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force }
    }
    Get-ChildItem (Join-Path $PgRoot 'lib') -Filter '*.lib' -ErrorAction SilentlyContinue |
        Remove-Item -Force
    $locale = Join-Path $PgRoot 'share/locale'
    if (Test-Path $locale) { Remove-Item $locale -Recurse -Force }
    Get-ChildItem (Join-Path $PgRoot 'bin') -Filter '*.exe' |
        Where-Object { $KEEP_EXE -notcontains $_.Name } | Remove-Item -Force
    # wxWidgets is StackBuilder's GUI toolkit and is 11MB of nothing the server
    # loads.
    Get-ChildItem (Join-Path $PgRoot 'bin') -Filter 'wx*.dll' -ErrorAction SilentlyContinue |
        Remove-Item -Force
}

# --- assemble ---------------------------------------------------------------

if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Out = (Resolve-Path $Out).Path

Write-Output 'staging PostgreSQL'
$pgRoot = Join-Path $Out 'pgsql'
Get-Postgres -Destination $pgRoot
Remove-Unshipped -PgRoot $pgRoot

Write-Output 'adding pgvector'
# Laid into the Postgres tree at the paths the server looks for them: an
# extension is $libdir\vector.dll plus its control and SQL under
# share\extension. Copying rather than symlinking, because the installer is
# going to copy anyway and a link would not survive it.
$vectorDll = Get-ChildItem $PgvectorDir -Recurse -Filter 'vector.dll' | Select-Object -First 1
if (-not $vectorDll) { throw "no vector.dll under $PgvectorDir" }
Copy-Item $vectorDll.FullName (Join-Path $pgRoot 'lib')
$extSrc = Join-Path (Split-Path (Split-Path $vectorDll.FullName)) 'share/extension'
Copy-Item (Join-Path $extSrc 'vector*') (Join-Path $pgRoot 'share/extension')
if (-not (Test-Path (Join-Path $pgRoot 'share/extension/vector.control'))) {
    throw 'vector.control did not make it into the payload'
}

Write-Output 'adding the server binary'
# The frozen tree goes at the TOP of the payload, not in a subdirectory:
# engraphy_win.install_root() is the directory holding engraphy-win.exe, and
# pgsql\ has to be its sibling.
Copy-Item (Join-Path $ServerDir '*') $Out -Recurse -Force

Write-Output 'adding the model cache'
Copy-Item $ModelDir (Join-Path $Out 'model') -Recurse -Force

Write-Output 'adding the scripts the installer runs'
New-Item -ItemType Directory -Force -Path (Join-Path $Out 'deploy') | Out-Null
Copy-Item (Join-Path $repo 'deploy/provision-app-role.sql') (Join-Path $Out 'deploy')
Copy-Item (Join-Path $here 'register-task.ps1') $Out
Copy-Item (Join-Path $here 'engraphy-task.xml') $Out

# --- report -----------------------------------------------------------------

$total = (Get-ChildItem $Out -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Output ''
Write-Output ("payload: {0:N0} files, {1:N1} MB" -f `
    (Get-ChildItem $Out -Recurse -File).Count, ($total / 1MB))
foreach ($d in (Get-ChildItem $Out -Directory)) {
    $s = (Get-ChildItem $d.FullName -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Output ("  {0,-16} {1,8:N1} MB" -f $d.Name, ($s / 1MB))
}
