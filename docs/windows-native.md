# Engraphy on Windows without Docker

**2026-09-09.** A design and a working prototype for a Windows distribution that
a non-developer installs once and then does not think about again: no Docker, no
Python, no toolchain, no daily ritual. Everything below is either built and
proved in CI or measured on a real machine, and the parts that are neither are
named as such.

The audience is a consultant on an 8GB laptop whose job is not writing software.
That single fact decides most of what follows.

## Contents

1. [The shape](#1-the-shape)
2. [PostgreSQL and pgvector on native Windows](#2-postgresql-and-pgvector-on-native-windows)
3. [The server as a shipped binary](#3-the-server-as-a-shipped-binary)
4. [Zero daily friction: the lifecycle](#4-zero-daily-friction-the-lifecycle)
5. [One installer](#5-one-installer)
6. [The measured footprint](#6-the-measured-footprint)
7. [What is proved, and what is not](#7-what-is-proved-and-what-is-not)
8. [Decisions that are not ours to make](#8-decisions-that-are-not-ours-to-make)

## 1. The shape

```
%LOCALAPPDATA%\Programs\Engraphy\        replaced wholesale by an upgrade
    engraphy-win.exe                     the only executable anything calls
    _internal\                           the frozen Python runtime and libraries
    pgsql\bin | lib | share              PostgreSQL 16.15, trimmed, plus vector.dll
    model\                               the embedding graph, baked at build time
    register-task.ps1, engraphy-task.xml

%LOCALAPPDATA%\Engraphy\                 an upgrade never touches this
    pgdata\                              the cluster
    backups\                             the pre-migration dumps
    logs\
    engraphy.json                        ports, space, and the two DB passwords
```

One process tree, started by one Scheduled Task at logon:

```
engraphy-win.exe run
├── postgres.exe          started through pg_ctl, listening on 127.0.0.1:55432
└── the MCP server        in-process uvicorn, listening on 127.0.0.1:8000
```

The split between the two roots is the upgrade story. An installer may delete
and rewrite the program directory; everything a user would grieve for is on the
other side of that line.

## 2. PostgreSQL and pgvector on native Windows

### The constraint

pgvector publishes **no Windows binaries**. Its documented Windows build is
`nmake /F Makefile.win` inside an "x64 Native Tools Command Prompt for VS",
which is Visual Studio with the C++ workload: several gigabytes of developer
tooling on a machine whose owner does not write software. Third party prebuilt
DLLs exist. They are not an option, for two independent reasons: a native
library loaded into the database backend has to come from a build whose inputs
are visible, and it has to be built against the same Postgres major and MSVC
runtime as the server it loads into, or it fails to load, or loads and takes the
backend down.

### The answer

Build it once, in public, in CI. `windows-latest` already carries MSVC.
[`.github/workflows/pgvector-windows.yml`](../.github/workflows/pgvector-windows.yml)
pins pgvector by commit SHA and the PostgreSQL archive by exact version URL plus
sha256, builds with `nmake`, and publishes `vector.dll` with its control file,
its SQL, a `SHA256SUMS`, and a `build-info.json` recording what went in. Nobody
downstream compiles anything, and no third-party binary is trusted.

The build is not the interesting half. The workflow then creates a cluster from
the same PostgreSQL archive the installer bundles, loads the DLL that was just
built, and runs
[`deploy/windows/pgvector-probe.sql`](../deploy/windows/pgvector-probe.sql):
500 rows of `vector(384)`, an HNSW index on `vector_cosine_ops`, a
nearest-neighbour query, and an assertion that the plan actually used the index.
A DLL that compiles proves nothing about whether it loads.

### The version pairing

| | version | why this one |
|---|---|---|
| PostgreSQL | **16.15-3**, EDB Windows x64 binaries | `compose.yaml` runs `pgvector/pgvector:pg16`, the Dockerfile pins `postgresql-client-16`, and CI pins the same. A different major here would mean the Windows install and every other install are not the same product |
| pgvector | **0.8.6** | current, and the series the `pgvector/pgvector:pg16` image carries |
| MSVC | whatever `windows-latest` carries | recorded in `build-info.json` per build |

An extension binary is only valid for the major it was compiled against. These
four pins move together or none of them do, and the workflow refuses to proceed
if the archive hash or the pgvector commit is not what it expects.

### Where the Postgres comes from

EnterpriseDB publishes binaries-only Windows ZIPs, which is the distribution
postgresql.org points at for Windows. The archive unpacks to 883MB, of which
722MB is pgAdmin, a desktop database GUI. After
[`build-payload.ps1`](../deploy/windows/build-payload.ps1) removes the
applications, the documentation, the headers, the link libraries, the message
translations and the client tools nothing here runs, **66MB** remains. The keep
list is explicit rather than a deny list, because the failure mode of guessing
wrong is a tool missing at the moment an operator needs it most: `pg_dump` and
`pg_restore` stay, because `engraphy-admin migrate` takes an unconditional
pre-migration dump and that is what makes an upgrade recoverable.

### The cluster

`initdb` with `--auth-local=scram-sha-256 --auth-host=scram-sha-256`. The
Windows default is `trust` for local connections, which would let any process
running as that user connect as the superuser and read every memory in the
store. The two passwords are 32 bytes of `secrets.token_hex`, generated at
bootstrap, never shown to anyone and never typed, so requiring them costs the
user nothing.

It listens on **127.0.0.1:55432**, not 5432. A consultant's laptop may already
have a PostgreSQL from another product, and an installer that fought it for the
well-known port would either break that product or fail to start. Nothing
outside this install needs to find the port, so moving it costs nothing. The
port is chosen by binding at bootstrap and recorded in `engraphy.json`, so a
machine that already has 55432 taken gets the next free one.

The cluster is tuned the way `compose.small.yaml` tunes the container one:
`shared_buffers` 32MB, `max_connections` 20, `maintenance_work_mem` 32MB,
`effective_cache_size` 256MB, no parallel workers per gather, one autovacuum
worker. Autovacuum stays on. Those settings are measured in
[footprint-2026-09-09.md](footprint-2026-09-09.md) and are the second largest
saving in the stack.

## 3. The server as a shipped binary

Three options were considered, and the requirement that decided it is item 3
below rather than anything about size.

**PyInstaller, onedir. Chosen.** One executable, one directory, no interpreter
on the machine, and a lifetime independent of any window the user might close.
Onedir rather than onefile deliberately: a onefile build unpacks its whole
payload to a temporary directory on every start, and this payload carries ONNX
Runtime and a 34MB model graph. Inside an installer a single file buys nothing,
because the user sees an installer either way, and it costs a delay on every
start plus a `%TEMP%` tree for antivirus to inspect each time.

**Embedding the server in the Electron desktop app. Rejected.** It couples the
server's lifetime to a GUI, and the entire requirement is that the memory is up
whether or not anyone opened anything. A user who closes the window would stop
their own memory server, and the failure would look like the network being
down.

**A bundled Python runtime. Rejected, but it is the closest second.** It ships a
`python.exe` a user can invoke by accident, upgrades are a directory swap rather
than an atomic artifact, and the install then has a Python on it that Windows
will offer to open `.py` files with. What it would buy is a friendlier build:
`pip install engraphy` into an embedded distribution has no hidden-import
problem at all. If PyInstaller ever becomes the thing that breaks a release,
this is the fallback, and nothing above the process boundary would change.

### What freezing actually costs

A frozen build fails in ways a source run cannot, and all of them look identical
from outside: a server that starts and does nothing. So the binary carries a
`selftest` verb that imports the fragile pieces and **uses** them, and CI asserts
on its output against the artifact that ships:

```
engraphy-win selftest
  ok    migrations resolve as package data: schema 0024
  ok    pack schema resolves as package data: 5 top-level keys
  ok    uvicorn's runtime-named modules: 0.52.4
  ok    psycopg and its pool: 3.3.5 on binary, pool 3.3.1
  ok    the MCP server builds: engraphy.server.app imported
  ok    the embedding model: micro, 384 dimensions
  ok    the bundled PostgreSQL: pg_config (PostgreSQL) 16.15
  ok    the pgvector build: vector.dll and its SQL are present
```

It needs no database, which is what lets CI run it, and it is the first thing to
run on a laptop where something is wrong, because it separates a broken install
from a broken database in one command.

Three classes of packaging gap were found this way and closed in the spec:
uvicorn's event loop, protocol and lifespan modules, which are chosen by string
at runtime; `jsonschema`'s distribution metadata, which it reads at import to
resolve format checkers; and numpy 2.x's `_core` submodules, which are reached
through machinery the analysis cannot follow.

### The embedding profile

The distribution ships **`micro`** (gte-small int8). The whole stack measured
187MB resident with it against 983MB on the shipped defaults
([footprint-2026-09-09.md](footprint-2026-09-09.md)), and 8GB of RAM is the
machine this targets. This is a property of the distribution rather than a
runtime choice: a store written under one embedding profile has to keep being
read under it, so the model is baked into the payload and the profile is set by
`engraphy-win run` rather than left to the environment.

## 4. Zero daily friction: the lifecycle

### A logon Scheduled Task, not a Windows service

A service runs as SYSTEM or as a service account. That puts the cluster and the
model cache outside the user's profile, needs administrator rights at install
time, and needs an ACL the service account can write. Every one of those is a
way for a first boot to fail on a machine nobody can debug, and none of them
buys anything here: this serves one person, on their own laptop, while they are
logged in.

A logon task needs no elevation, runs as the user, and reads and writes the
user's own directories. What a service would add is that the stack would be up
before anyone logs in and would survive fast user switching. Neither matters to
a consultant who is the only account on their machine, and if it ever does, a
wrapper (WinSW, NSSM) is additive rather than a rewrite.

The task settings that matter, and why, are in
[`engraphy-task.xml`](../deploy/windows/engraphy-task.xml). Three of them are
Windows defaults that would otherwise break this outright:
`ExecutionTimeLimit` defaults to three days, after which Windows would stop the
memory server; `DisallowStartIfOnBatteries` and `StopIfGoingOnBatteries` both
default to true, which on a laptop means the memory stops working when it is
unplugged.

### Task Scheduler is the supervisor

`engraphy-win run` has no restart loop of its own. `RestartOnFailure` already
restarts a task that exits, with an interval and a count, and it does so across
a crash that would take a supervisor with it. A second layer of restart logic
would only be another thing to get wrong.

### Starting and stopping

```
engraphy-win run       start postgres, wait for it, serve. What the task runs
engraphy-win stop      end the task, then stop the cluster with a fast shutdown
engraphy-win status    what is running, on what port, and what /healthz says
```

`run` installs a Win32 console control handler for `CTRL_CLOSE`, `CTRL_LOGOFF`
and `CTRL_SHUTDOWN`, and stops the cluster on the way out. Python's `signal`
module maps `CTRL_C` and `CTRL_BREAK` and nothing else, so the three events that
actually happen at the end of a session need the Win32 handler directly. This is
why the binary is a **console** build run by a **hidden** task: a windowed build
would silently lose the shutdown notification. When the handler does not get its
few seconds, or the machine loses power, Postgres recovers from WAL on the next
start. Crash safety is a property of the database, not something this program
has to provide.

### Upgrades

The existing `version.json` manifest already carries a `downloads[]` array with
`platform`, `arch`, `sha256` and `signed` per product, and both clients read it
(`engraphy-desktop/src/main/versionCheck.ts`). A Windows stack release is a new
product key in that manifest with the installer as its download, which needs no
new mechanism and no new client code path.

The installer itself is the upgrade path:

1. stop the task and the server, so nothing holds a handle on the binaries
2. copy the new payload over the program directory
3. `engraphy-win upgrade`, which is `migrate`: an unconditional pre-migration
   `pg_dump` into `%LOCALAPPDATA%\Engraphy\backups`, then the schema advance
4. re-register the task, in case the install directory moved
5. leave the data directory, the token and the handoff alone

A failed upgrade therefore leaves a restorable store rather than a
half-migrated one, which is the same property `engraphy-admin migrate` gives
every other deployment.

### The token handoff

The installer creates the database, the space and the token before the desktop
app is ever opened. The one step left was moving a bearer token between two
windows by hand, which is the step a non-developer gets wrong.

So `engraphy-win token` writes `engraphy-bootstrap.json` beside the desktop
app's own settings file, and the app imports it on first launch through the
same `saveSettings` path a typed token takes, so it goes into the OS keychain
via `safeStorage` exactly as it would have. The file is deleted once stored, so
the plaintext lives for one launch. The import happens once, only onto an app
that has no token of its own, so a reinstall never replaces credentials already
in use. The URL is restricted to loopback: a handoff decides where a bearer
token gets sent, and nothing legitimate writes a remote host into a local
installer's handoff.

This reuses the existing connection mechanism rather than adding a second one.
A user who later re-mints a token or points at a different server wins over a
file an installer left months ago, which is why the handoff is imported and
deleted rather than read on every launch.

## 5. One installer

**NSIS**, per-user, no elevation. `RequestExecutionLevel user`, installing to
`%LOCALAPPDATA%\Programs\Engraphy`, with a per-user uninstall entry so Engraphy
appears in Settings > Apps without the install having needed administrator
rights.

| | what it would buy | what it would cost |
|---|---|---|
| **NSIS** (chosen) | lays down files and runs a command, which is exactly what this payload needs. `engraphy-desktop` already ships an NSIS target through electron-builder, so a Windows build means one installer toolchain rather than two | a hand-written uninstaller, and no Store distribution |
| Inno Setup | equally capable, arguably nicer scripting | a second toolchain in the same repo for no gain |
| **MSIX** | a clean uninstall, Store distribution, and signing that comes with the Store | it runs the payload in a container with a virtualised filesystem and registry, and its autostart is limited to declared extension points. A PostgreSQL cluster writing to a real data directory and a long-lived background process both fight that model rather than fit it |

The MSIX row is the one worth stating plainly: it is not a preference, it is
that the two things this product is made of are the two things the packaging
model constrains.

What the installer does is listed in
[`deploy/windows/README.md`](../deploy/windows/README.md). Every step is
idempotent, so a repair install and a reinstall over an existing store both do
the right thing. The uninstaller removes the program and the task and leaves
`%LOCALAPPDATA%\Engraphy` alone, telling the user where it is: that directory
holds the one thing here that cannot be reinstalled.

## 6. The measured footprint

To be completed against the built artifact.

## 7. What is proved, and what is not

Kept separate on purpose. A design document that reads as though everything in
it were tested is worth less than one that says where the edge is.

### Proved, by running it

| | where |
|---|---|
| pgvector 0.8.6 builds against PostgreSQL 16.15 with MSVC, loads into a cluster made from the same archive, and answers a 384-dimension cosine query through an HNSW index the plan actually uses | `pgvector-windows.yml`, green; and repeated on this Windows 11 laptop against the CI-built DLL |
| The frozen binary carries everything it needs: package data, uvicorn's runtime-named modules, psycopg's libpq binding, the MCP server, and an embedding graph it runs to a real vector | `engraphy-win selftest --binary-only` in `windows-dist.yml`, asserted line by line |
| `bootstrap` end to end: `initdb` with scram authentication, the laptop Postgres tuning, `CREATE DATABASE`, all 24 migrations behind their unconditional pre-dump, the `engraphy_app` role, and a space with its restore sentinel | run on this laptop against the shipped payload |
| The Postgres tuning is what lands, not what was intended | `pg_settings` read back after the workload: `shared_buffers` 32MB, `max_connections` 20, `maintenance_work_mem` 32MB, `effective_cache_size` 256MB |
| Port collision avoidance is real rather than theoretical | bootstrap took **8001** on this machine, because a live Engraphy already held 8000 |
| The logon task registers with the settings it is supposed to have and unregisters cleanly | registered, read back and removed on this laptop |
| The whole distribution builds from a clean checkout into one installer | `windows-dist.yml`, green: 80.9 MB installer from a 258.8 MB payload |

### Not proved

- **The installer has not been run on a clean Windows machine.** It builds, and
  every step it performs is exercised individually above, but the sequence of
  them under NSIS on a machine that has never had Engraphy is untested. That is
  the single highest-value thing to do next.
- **Uninstall and upgrade-over-an-existing-store are untested as file
  operations.** The migration half of an upgrade is exercised; replacing a
  running install's binaries is not.
- **No cold boot.** The task is registered and starts on demand; a real logoff
  and logon cycle, and the console control handler's shutdown path, have not
  been observed.
- **SmartScreen behaviour is unmeasured** because the installer is unsigned. See
  section 8.
- **The desktop handoff is unit-tested, not end-to-end.** `parseBootstrap` and
  the import rules are covered by the desktop suite; a packaged desktop build
  importing a file a real installer wrote has not been run.

## 8. Decisions that are not ours to make

**Code signing, and it is not cosmetic.** An unsigned installer with no
reputation gets "Windows protected your PC" from SmartScreen, and the user has
to click More info and then Run anyway. That is exactly the friction this design
exists to remove, and it lands on the first thirty seconds of the experience. An
OV certificate is the cheaper option and builds reputation over time; an EV
certificate gets SmartScreen trust immediately and needs a hardware token.
Either way it is a purchase and an identity verification, so it is a decision
rather than a task. Everything else here works without it.

**Where the installer is hosted.** GitHub Releases is what `build-version.py`
already reads for the desktop app, so attaching the installer to a release means
the manifest generator learns one new asset pattern and nothing else changes. A
download on engraphy.tech would need its own publishing step.

**Whether the Windows stack gets its own product key in `version.json`.** It is a
fourth independently versioned thing next to the engine, the extension and the
desktop app. The manifest is built for exactly this, and the only judgement is
its `minimumSupported` floor, which nothing publishes and which is reviewed per
release.

**NSIS or MSIX, if Store distribution ever matters.** Section 5 is a technical
recommendation, not a business one. MSIX would mean reworking the data directory
and the autostart around the packaging model, and it would bring signing with
it.

**Whether `micro` is permanent for Windows.** The distribution bakes gte-small
int8, and a store written under one embedding profile has to keep being read
under it. Changing later means a re-embed on every installed machine
([micro-reembed.md](micro-reembed.md) is the procedure), so this is worth
settling before it is on laptops rather than after.

**Whether a service ever replaces the logon task.** Only if the stack has to be
up before anyone logs in, or has to survive fast user switching. Neither is
true for one consultant on their own laptop, and the change is additive if it
becomes true.

**Whether the desktop app ships inside the same installer.** They are separate
today, which means two downloads for one product. Combining them is a packaging
decision with a real cost: the desktop app updates from the Microsoft Store on
one channel and from a direct download on the other, and folding it in would
have to pick one.
