# Building the Windows distribution

The Docker-free Windows install: one executable that lays down PostgreSQL,
pgvector, the Engraphy server and the embedding model, creates the database,
and registers a logon task, on a machine with no Python, no Visual Studio and
no Docker.

The design and the measurements are in [`docs/windows-native.md`](../../docs/windows-native.md).
This file is how the artifact is produced.

## The short version

Everything is built by `.github/workflows/windows-dist.yml`. Run it from the
Actions tab, or let a pull request touching `deploy/windows/**` run it, and
download the `engraphy-windows-installer` artifact. Pass a `release_tag` to
attach the installer to a release instead.

Nothing below has to be run by hand. It is written down because a build nobody
can reproduce locally is a build nobody can debug.

## What is in the box

| file | what it is |
|---|---|
| `engraphy_win.py` | the program the installer, the task and the Start menu all call. Owns the cluster as well as the server |
| `engraphy-win.spec` | the PyInstaller spec. Onedir, console, model excluded from the binary |
| `build-payload.ps1` | assembles the install directory: Postgres trimmed to 66MB, pgvector laid in, the frozen server, the model |
| `installer.nsi` | the NSIS installer, per-user, no elevation |
| `register-task.ps1` | registers or removes the logon task |
| `engraphy-task.xml` | that task's definition, and why each setting is what it is |
| `pgvector-probe.sql` | the query that proves a freshly built `vector.dll` works |

## Building it by hand

Three inputs, in this order. Each one is independent of the next apart from the
artifact it produces.

### 1. pgvector

Needs Visual Studio with the C++ workload, which is the reason this is normally
left to CI:

```
gh workflow run pgvector-windows.yml
gh run download <run-id> -n pgvector-0.8.6-pg16-windows-x64 -D artifacts
```

The workflow pins pgvector by commit SHA and PostgreSQL by version URL plus
sha256, builds with `nmake /F Makefile.win`, and then loads the result into a
real cluster and runs a 384-dimension cosine query through an HNSW index. A DLL
that compiles is not evidence that it loads.

### 2. The server binary

Needs Python 3.12 on Windows. Not 3.13 or later, and not the version that
happens to be on the machine: the frozen binary carries the interpreter it was
built with, so this decides what ships.

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install . pyinstaller==6.11.1

$env:ENGRAPHY_EMBEDDING_PROFILE = 'micro'
$env:HF_HOME = "$PWD\build\windows\model"
python -c "from engraphy.core import embedding; embedding.embed_document('prebake')"

pyinstaller --clean --noconfirm deploy/windows/engraphy-win.spec
```

The prebake goes through `embedding.embed_document`, the same seam runtime
uses, so the cache holds exactly what runtime fetches at the pinned revision
rather than a hand-guessed subset. The profile is `micro` because that is what
the distribution ships, and a store written under one profile has to keep being
read under it.

Then prove the build:

```
$env:ENGRAPHY_WIN_ROOT = "$PWD\dist\engraphy-win"
.\dist\engraphy-win\engraphy-win.exe selftest
```

Two of its checks fail at this point by design: the bundled PostgreSQL and the
pgvector build are not in a bare PyInstaller output. Everything above them
should pass, and those are the ones that catch a frozen build missing a hidden
import or a data file.

### 3. The payload and the installer

```
./deploy/windows/build-payload.ps1 `
    -PgvectorDir artifacts -ServerDir dist/engraphy-win -ModelDir build/windows/model `
    -Out build/windows/payload

makensis /DVERSION=0.2.0 "/DPAYLOAD=$PWD\build\windows\payload" deploy/windows/installer.nsi
```

`build-payload.ps1` downloads the PostgreSQL archive itself and verifies its
sha256, or takes one already downloaded with `-PgZip`. It prints the size of
each part of the payload, which is the number to watch: the trim is what keeps
883MB of EDB archive down to 66MB of Postgres.

## What the installer does on the user's machine

1. Stops anything already running, so an upgrade is not overwriting open files.
2. Copies the payload to `%LOCALAPPDATA%\Programs\Engraphy`.
3. `engraphy-win bootstrap`: `initdb` with scram authentication and generated
   passwords, the laptop Postgres tuning, `CREATE DATABASE`, the migrations
   with their unconditional pre-dump, the `engraphy_app` role and its grants,
   a space and the starter pack.
4. `engraphy-win upgrade`, which is a no-op on a fresh install and the schema
   advance on an upgrade.
5. Registers the logon task and starts it.
6. `engraphy-win token`, which mints a token and writes the handoff file the
   desktop app imports on first launch.

Every step is idempotent, so a repair install and a reinstall over an existing
store both do the right thing. The uninstaller removes the program and the
task and leaves `%LOCALAPPDATA%\Engraphy` alone, because that is where the
memories are.

## Signing

The installer is unsigned. SmartScreen shows "Windows protected your PC" on
first run and the user has to click through it, which is exactly the friction
this design exists to remove, so this is a real gap rather than a cosmetic one.
See the decisions list in [`docs/windows-native.md`](../../docs/windows-native.md).
