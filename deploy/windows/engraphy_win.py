"""engraphy-win: the whole Engraphy stack as one Windows program.

This is the entry point PyInstaller freezes into `engraphy-win.exe`, and it is
the only executable the installer, the Scheduled Task and the Start menu ever
call. It owns the bundled PostgreSQL cluster as well as the server process,
because on a consultant's laptop those are not two things to think about: there
is either a memory that answers or there is not.

    engraphy-win bootstrap   first run: create the cluster, migrate, provision
                             the app role, create a space, mint a token
    engraphy-win run         the supervised foreground process: start Postgres,
                             wait for it, serve. This is what the logon task runs
    engraphy-win stop        stop the server and the cluster, in that order
    engraphy-win status      what is running, what version, what port
    engraphy-win token       mint a fresh client token and hand it to the app
    engraphy-win upgrade     after a new build is laid down: migrate forward
    engraphy-win selftest    prove the install is complete, no database needed

WHERE THINGS LIVE

    <install root>\\            program files, replaced wholesale by an upgrade
      pgsql\\                   the bundled PostgreSQL 16 plus vector.dll
      engraphy-win.exe        this program
      model\\                   the embedding model, baked at build time
    %LOCALAPPDATA%\\Engraphy\\   per-user state, which an upgrade never touches
      pgdata\\                  the cluster
      backups\\                 the pre-migration dumps `migrate` always takes
      logs\\
      engraphy.json           config, including the two database passwords

The split is the upgrade story: an installer may delete and rewrite the install
root, and everything the user would grieve for is on the other side of that
line.

WHY LOCALAPPDATA AND NOT PROGRAMDATA

Per-user, which is the same decision as running under a logon task rather than a
service. A machine-wide install would need administrator rights at every step, a
service account, and an ACL on the data directory that the account can write and
the user cannot read, and it would still only ever serve one person. The whole
product here is one consultant's own memory.

The two database passwords are generated at bootstrap and stored in
engraphy.json. That file is created with inheritance removed and a single ACE
for the installing user, which is the protection `%APPDATA%` gives a `.pgpass`
and what the OS offers short of asking a non-developer to manage a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import time

# The bundled Postgres listens here rather than on 5432. A consultant's laptop
# may already have a PostgreSQL from some other product, and an installer that
# fought it for the well-known port would break that product, or fail to start,
# depending on which won. Nothing outside this install needs to find the port,
# so it costs nothing to move.
DEFAULT_PG_PORT = 55432
DEFAULT_SERVER_PORT = 8000
DEFAULT_SPACE = "personal"
DEFAULT_PRINCIPAL = "me"

# How long to wait for a freshly started postmaster to accept connections.
# Generous because the first start after an unclean shutdown replays WAL.
PG_READY_TIMEOUT_S = 60


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

def install_root() -> pathlib.Path:
    """The directory holding this program and the payload beside it.

    Frozen, `sys.executable` is `<root>\\engraphy-win.exe`. Unfrozen (running
    from a checkout for development), this file is `deploy/windows/`, and the
    payload is wherever ENGRAPHY_WIN_ROOT says. There is no useful guess for the
    unfrozen case, so it is an explicit variable rather than a path walk that
    would silently resolve to the wrong tree.
    """
    override = os.environ.get("ENGRAPHY_WIN_ROOT")
    if override:
        return pathlib.Path(override)
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    raise SystemExit(
        "running from source: set ENGRAPHY_WIN_ROOT to the directory holding pgsql\\")


def data_root() -> pathlib.Path:
    override = os.environ.get("ENGRAPHY_WIN_DATA")
    if override:
        return pathlib.Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise SystemExit("LOCALAPPDATA is not set, so there is nowhere to put the data")
    return pathlib.Path(base) / "Engraphy"


def pg_bin(name: str) -> pathlib.Path:
    return install_root() / "pgsql" / "bin" / (name + ".exe")


def config_path() -> pathlib.Path:
    return data_root() / "engraphy.json"


def read_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"no configuration at {config_path()}. Run `engraphy-win bootstrap` first.")


def write_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    _lock_down(path)


def _lock_down(path: pathlib.Path) -> None:
    """Remove inherited ACEs and grant only the current user.

    Windows has no chmod, and `os.chmod` sets the read-only attribute rather
    than an ACL, so it does nothing useful for a file holding a password. icacls
    ships with Windows. A failure here is reported and not fatal: the file is
    already inside the user's own profile, so this narrows an existing
    protection rather than creating the only one.
    """
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                       check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"warning: could not restrict permissions on {path}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Postgres lifecycle
# --------------------------------------------------------------------------

def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a bundled tool with the payload's bin on PATH.

    libpq and the ICU DLLs sit beside the executables in `pgsql\\bin`, and
    `engraphy-admin migrate` shells out to `pg_dump` by name. Putting the
    directory on PATH for children covers both without the caller thinking
    about it.
    """
    env = dict(kw.pop("env", os.environ))
    env["PATH"] = str(install_root() / "pgsql" / "bin") + os.pathsep + env.get("PATH", "")
    # check defaults to False: every caller here inspects returncode itself and
    # turns a failure into a message a non-developer can act on, which a
    # CalledProcessError traceback is not.
    check = kw.pop("check", False)
    return subprocess.run(cmd, env=env, check=check, **kw)


def pg_is_running(pgdata: pathlib.Path) -> bool:
    r = _run([str(pg_bin("pg_ctl")), "status", "-D", str(pgdata)],
             capture_output=True, text=True)
    # pg_ctl status exits 0 when running, 3 when not, and 4 when the data
    # directory is unusable. Only 0 is "running".
    return r.returncode == 0


def pg_start(cfg: dict) -> None:
    """Start the cluster, and return once it is accepting connections.

    pg_ctl's own output goes to a FILE, never to a pipe. `pg_ctl start`
    daemonises the postmaster, which inherits whatever handles pg_ctl was given:
    with `capture_output=True` the postmaster holds the read end of that pipe for
    as long as it runs, and this process waits on EOF forever. pg_ctl exits, the
    database comes up, and the caller never returns. Under the installer that is
    a setup step that hangs on first install with everything apparently working.

    A file handle has no such problem, and the diagnostics survive rather than
    being thrown away, which redirecting to DEVNULL would also have done.
    """
    pgdata = pathlib.Path(cfg["pgdata"])
    if pg_is_running(pgdata):
        return
    logs = pathlib.Path(cfg["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    ctl_log = logs / "pg_ctl.log"
    options = f"-p {cfg['pg_port']} -c listen_addresses=127.0.0.1"
    with open(ctl_log, "w", encoding="utf-8") as sink:
        r = _run([str(pg_bin("pg_ctl")), "start", "-D", str(pgdata),
                  "-l", str(logs / "postgres.log"), "-o", options, "-w",
                  "-t", str(PG_READY_TIMEOUT_S)],
                 stdout=sink, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        detail = ctl_log.read_text(encoding="utf-8", errors="replace").strip()
        server_log = logs / "postgres.log"
        if server_log.exists():
            detail += "\n" + server_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise SystemExit(f"postgres did not start:\n{detail}")


def pg_stop(cfg: dict) -> None:
    pgdata = pathlib.Path(cfg["pgdata"])
    if not pg_is_running(pgdata):
        return
    # `fast` rather than `smart`: smart waits for clients to disconnect, and at
    # logoff there is no one left to disconnect them. fast rolls back open
    # transactions and checkpoints, so the next start is a clean start rather
    # than a WAL replay.
    # Same file-not-pipe reasoning as pg_start. `stop -w` waits for the
    # postmaster to exit so nothing should outlive the pipe, but a backend that
    # refuses to die would turn a shutdown into a hang, and at logoff there are
    # seconds rather than minutes to play with.
    logs = pathlib.Path(cfg["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "pg_ctl.log", "a", encoding="utf-8") as sink:
        _run([str(pg_bin("pg_ctl")), "stop", "-D", str(pgdata), "-m", "fast", "-w"],
             stdout=sink, stderr=subprocess.STDOUT)


def wait_for_pg(cfg: dict, timeout_s: int = PG_READY_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = _run([str(pg_bin("pg_isready")), "-h", "127.0.0.1",
                  "-p", str(cfg["pg_port"]), "-q"], capture_output=True)
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise SystemExit(f"postgres was not accepting connections after {timeout_s}s")


# --------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------

def _free_port(preferred: int) -> int:
    """`preferred` if nothing holds it, otherwise the next free port above it.

    Checked by binding rather than by connecting: a connect that is refused
    means nothing is listening right now, which is not the same as the port
    being available to bind.
    """
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"no free port in {preferred}..{preferred + 49}")


def _conninfo(cfg: dict, *, superuser: bool) -> str:
    user = "postgres" if superuser else "engraphy_app"
    pw = cfg["superuser_password"] if superuser else cfg["app_password"]
    return (f"postgres://{user}:{pw}@127.0.0.1:{cfg['pg_port']}/engraphy"
            f"?sslmode=disable")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Everything between "files are on disk" and "a client can write a memory".

    Idempotent by construction, because an installer re-run and a repair install
    both land here: each step checks whether it has already happened. The one
    thing it deliberately does NOT redo is minting a token, since a second token
    for the same client name is an error rather than a no-op.
    """
    root = install_root()
    data = data_root()
    pgdata = data / "pgdata"

    if config_path().exists():
        cfg = read_config()
    else:
        cfg = {
            "pgdata": str(pgdata),
            "logs": str(data / "logs"),
            "backups": str(data / "backups"),
            "pg_port": _free_port(args.pg_port),
            "server_port": _free_port(args.server_port),
            "space": args.space,
            "principal": args.principal,
            # 32 bytes of urandom rendered as hex. Never shown to the user and
            # never typed by anyone, so there is no reason for it to be short.
            "superuser_password": secrets.token_hex(32),
            "app_password": secrets.token_hex(32),
        }
        write_config(cfg)

    for key in ("logs", "backups"):
        pathlib.Path(cfg[key]).mkdir(parents=True, exist_ok=True)

    if not (pgdata / "PG_VERSION").exists():
        print(f"creating the database cluster in {pgdata}")
        pgdata.mkdir(parents=True, exist_ok=True)
        pwfile = data / "initdb.pw"
        pwfile.write_text(cfg["superuser_password"], encoding="utf-8")
        _lock_down(pwfile)
        try:
            # scram-sha-256 for host AND local. initdb's default on Windows is
            # `trust` for local connections, which would let any process running
            # as this user connect as the superuser and read every memory in the
            # store. The password is generated, stored once, and never typed, so
            # requiring it costs the user nothing.
            r = _run([str(pg_bin("initdb")), "-D", str(pgdata), "-U", "postgres",
                      f"--pwfile={pwfile}", "-E", "UTF8", "--locale=C",
                      "--auth-local=scram-sha-256", "--auth-host=scram-sha-256"],
                     capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"initdb failed: {r.stdout}{r.stderr}")
        finally:
            with contextlib.suppress(OSError):
                pwfile.unlink()
        _tune_postgresql_conf(pgdata)

    _install_pgvector(root, pgdata)

    pg_start(cfg)
    wait_for_pg(cfg)

    _ensure_database(cfg)
    _migrate(cfg)
    _provision_app_role(root, cfg)
    _ensure_space(cfg)

    print(f"bootstrap complete: space '{cfg['space']}' on 127.0.0.1:{cfg['server_port']}")
    return 0


def _tune_postgresql_conf(pgdata: pathlib.Path) -> None:
    """The same settings compose.small.yaml applies, written into the cluster.

    Measured 2026-09-09 (docs/footprint-2026-09-09.md): these take Postgres from
    111MB to 46MB resident on a 20,000-node store, and the vector leg measured
    0.15 ms/query against 0.21 ms/query on the defaults. A personal memory is not
    a database server, and `shared_buffers` at the 128MB default is reserved
    whether or not a few thousand rows need it.

    Appended to postgresql.conf rather than written as postgresql.auto.conf, so
    an operator who wants to change one can see it, edit it, and have the edit
    survive. Autovacuum stays on: turning it off would flatter a footprint
    measurement and leave the store to bloat.
    """
    conf = pgdata / "postgresql.conf"
    marker = "# --- Engraphy laptop profile ---"
    text = conf.read_text(encoding="utf-8")
    if marker in text:
        return
    conf.write_text(text + f"""
{marker}
# See docs/footprint-2026-09-09.md for the measurements behind each value.
shared_buffers = 32MB
max_connections = 20
maintenance_work_mem = 32MB
effective_cache_size = 256MB
max_parallel_workers_per_gather = 0
autovacuum_max_workers = 1
""", encoding="utf-8")


def _install_pgvector(root: pathlib.Path, pgdata: pathlib.Path) -> None:
    """Fail loudly when the extension is missing rather than at first search.

    The DLL and its SQL are laid down by the installer into the bundled Postgres
    tree, built by .github/workflows/pgvector-windows.yml against this exact
    Postgres major. Checking here means a broken payload is a message at install
    time, not an ENGRAPHY error the first time someone tries to remember
    something.
    """
    del pgdata
    dll = root / "pgsql" / "lib" / "vector.dll"
    control = root / "pgsql" / "share" / "extension" / "vector.control"
    missing = [str(p) for p in (dll, control) if not p.exists()]
    if missing:
        raise SystemExit("the pgvector build is not in the payload: " + ", ".join(missing))


def _ensure_database(cfg: dict) -> None:
    import psycopg
    admin = (f"postgres://postgres:{cfg['superuser_password']}@127.0.0.1:"
             f"{cfg['pg_port']}/postgres?sslmode=disable")
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = 'engraphy'").fetchone()
        if not exists:
            conn.execute("CREATE DATABASE engraphy")


def _migrate(cfg: dict) -> None:
    from engraphy.admin import migrate
    # migrate.run takes its unconditional pre-migration dump through pg_dump,
    # which resolves from the bundled bin because _run puts it on PATH. On a
    # brand new cluster that dump is nearly empty, which is fine and is not a
    # reason to have a skip flag.
    os.environ["PATH"] = (str(install_root() / "pgsql" / "bin") + os.pathsep
                          + os.environ.get("PATH", ""))
    result = migrate.run(_conninfo(cfg, superuser=True),
                         dump_dir=pathlib.Path(cfg["backups"]))
    for line in result.get("log", []):
        print(f"  {line}")


def _provision_app_role(root: pathlib.Path, cfg: dict) -> None:
    """Create engraphy_app and its grants, from the SQL the repo already ships.

    Run through the bundled psql rather than reimplemented here: the file uses
    psql's `:'app_role_password'` substitution, and having two implementations of
    the privilege model is how they drift. The server connects as this role
    precisely so that row-level security constrains it, which it cannot do for a
    superuser.
    """
    sql = root / "deploy" / "provision-app-role.sql"
    if not sql.exists():
        raise SystemExit(f"missing {sql}")
    # EVERY OPTION BEFORE THE CONNECTION STRING, and the connection string behind
    # -d rather than as a bare positional.
    #
    # psql's own getopt does not permute arguments on Windows, so anything after
    # the first positional is discarded:
    #     psql: warning: extra command-line argument "--file" ignored
    # and psql then reads an empty stdin and EXITS 0. The role is never created,
    # nothing reports a failure, and the first sign of it is the server failing
    # to authenticate as engraphy_app at boot, which Postgres reports as a
    # password failure rather than a missing role. GNU getopt permutes, which is
    # why the same ordering has always worked from the Linux containers.
    r = _run([str(pg_bin("psql")),
              "--set", "ON_ERROR_STOP=1",
              "--set", f"app_role_password={cfg['app_password']}",
              "--file", str(sql),
              "-d", _conninfo(cfg, superuser=True)],
             capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"provisioning the app role failed: {r.stdout}{r.stderr}")

    # Exit 0 is not evidence, as the above proves. Read the role back.
    import psycopg
    with psycopg.connect(_conninfo(cfg, superuser=True)) as conn:
        row = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles"
            " WHERE rolname = 'engraphy_app'").fetchone()
    if row is None:
        raise SystemExit(
            "the engraphy_app role does not exist after provisioning."
            f" psql said: {r.stdout}{r.stderr}")
    can_login, is_super, bypasses_rls = row
    if not can_login or is_super or bypasses_rls:
        # The server runs as this role precisely so row-level security
        # constrains it, which it cannot do for a superuser or a BYPASSRLS role.
        raise SystemExit(
            f"engraphy_app is provisioned wrongly: login={can_login}"
            f" superuser={is_super} bypassrls={bypasses_rls}")


def _ensure_space(cfg: dict) -> None:
    """Create the default space and give it the starter ontology.

    A store with no space is a store where every tool call fails on a foreign
    key, so "install once and it just runs" has to include this. Skipped when the
    space is already there, which is what makes a repair install safe.
    """
    import psycopg

    from engraphy.admin import cli as admin_cli
    conninfo = _conninfo(cfg, superuser=True)
    with psycopg.connect(conninfo) as conn:
        row = conn.execute("SELECT 1 FROM spaces WHERE id = %s", (cfg["space"],)).fetchone()
    if row:
        return
    os.environ["ENGRAPHY_DATABASE_URL"] = conninfo
    admin_cli.space_create(id=cfg["space"], display_name=cfg["space"].title(),
                           principal=cfg["principal"], principal_display_name=None,
                           database_url=conninfo)
    # Resolved through the packs package rather than from the install root: the
    # starter pack is frozen into the binary as package data, at the path
    # packs.py already resolves its schema from, so there is one answer to
    # "where do the shipped packs live" rather than two that can disagree.
    from engraphy.admin import packs as packs_mod
    pack = packs_mod.SCHEMA_PATH.parent / "starter" / "pack.yaml"
    if pack.exists():
        # `file` is the parameter name: pack_apply takes it as a positional
        # typer Argument, not as --path.
        admin_cli.pack_apply(file=str(pack), space=cfg["space"], database_url=conninfo)
    else:
        print(f"  no starter pack at {pack}, space created without an ontology")


# --------------------------------------------------------------------------
# tokens, and the handoff to the clients
# --------------------------------------------------------------------------

def cmd_token(args: argparse.Namespace) -> int:
    """Mint a client token and leave it where the desktop app will find it.

    The token is a secret, and the one thing a non-developer must not be asked to
    do is copy one between two windows correctly. The desktop app already owns a
    settings file and already encrypts the token into the OS keychain when it
    saves one, so this writes a single-use handoff file next to that settings
    file and lets the app import it on next launch. The app deletes the file once
    it has stored the token, so the plaintext lives for one launch rather than
    forever.
    """
    import psycopg

    from engraphy.server.auth import mint_token

    cfg = read_config()
    pg_start(cfg)
    wait_for_pg(cfg)

    async def _mint() -> str:
        async with await psycopg.AsyncConnection.connect(
                _conninfo(cfg, superuser=True)) as conn:
            raw, _meta = await mint_token(conn, cfg["space"], cfg["principal"],
                                          args.client_name, "readwrite")
            await conn.commit()
            return raw

    raw = asyncio.run(_mint())

    handoff = {
        "schema": 1,
        "serverUrl": f"http://127.0.0.1:{cfg['server_port']}/mcp/",
        "space": cfg["space"],
        "token": raw,
    }
    if args.write_handoff:
        path = _handoff_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(handoff, indent=2), encoding="utf-8")
        _lock_down(path)
        print(f"handoff written to {path}")
    if args.print_token:
        print(raw)
    return 0


def _handoff_path() -> pathlib.Path:
    """Beside the desktop app's own settings file.

    `%APPDATA%\\Engraphy` is `app.getPath('userData')` for a product named
    Engraphy, which is where engraphy-desktop keeps engraphy-settings.json. Two
    different roots by design: this is the roaming profile the app reads, and the
    stack's own state is in the local one, which should not roam.
    """
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("APPDATA is not set")
    return pathlib.Path(base) / "Engraphy" / "engraphy-bootstrap.json"


# --------------------------------------------------------------------------
# the supervised process
# --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    """Start Postgres, wait for it, then serve. What the logon task runs.

    Deliberately not a supervisor loop. Task Scheduler already restarts a task
    that exits with a failure, with a delay and a retry count, and it does it
    across a logoff and after a crash that would take a supervisor with it. A
    second layer of restart logic here would only be an extra thing to get wrong.

    Postgres is stopped on the way out. Windows sends a console control event to
    a console process at logoff and shutdown, and gives it a few seconds, which
    is enough for a fast shutdown checkpoint. When it is not enough, or when the
    machine loses power, Postgres recovers from WAL on next start: crash safety
    is a property of the database, not something this program has to provide.
    """
    del args
    cfg = read_config()
    _install_console_handler(cfg)
    pg_start(cfg)
    wait_for_pg(cfg)
    try:
        os.environ["ENGRAPHY_DATABASE_URL"] = _conninfo(cfg, superuser=False)
        os.environ["ENGRAPHY_BIND_HOST"] = "127.0.0.1"
        os.environ["ENGRAPHY_BIND_PORT"] = str(cfg["server_port"])
        from engraphy.server import app as server_app
        server_app.main()
    finally:
        pg_stop(cfg)
    return 0


def _install_console_handler(cfg: dict) -> None:
    """Stop Postgres on CTRL_CLOSE, CTRL_LOGOFF and CTRL_SHUTDOWN.

    Python's signal module maps CTRL_C and CTRL_BREAK and nothing else, so the
    three events that actually happen to a background task at the end of a
    session need the Win32 handler directly. The callback has to stay referenced
    for the life of the process or CPython will collect it and Windows will call
    into freed memory.
    """
    CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6
    # BOOL is a 4-byte int in the Win32 ABI, not C++'s one-byte bool. Declaring
    # the return as c_bool leaves three bytes of the register undefined, and
    # whether Windows then reads the handler as having consumed the event is
    # up to whatever was in them.
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)

    def _handler(event: int) -> int:
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            with contextlib.suppress(Exception):
                pg_stop(cfg)
        return 0  # FALSE: let the default handler end the process

    callback = handler_type(_handler)
    _install_console_handler.keepalive = callback  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)


# --------------------------------------------------------------------------
# the rest of the verbs
# --------------------------------------------------------------------------

def cmd_stop(args: argparse.Namespace) -> int:
    del args
    cfg = read_config()
    # check=False: no task registered is a normal state, not a failure. The
    # cluster still gets stopped below either way.
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True, check=False)
    pg_stop(cfg)
    print("stopped")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    del args
    import urllib.error
    import urllib.request
    cfg = read_config()
    running = pg_is_running(pathlib.Path(cfg["pgdata"]))
    print(f"postgres      {'running' if running else 'stopped'} on 127.0.0.1:{cfg['pg_port']}")
    url = f"http://127.0.0.1:{cfg['server_port']}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            print(f"engraphy      {r.status} {r.read().decode('utf-8')}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"engraphy      not answering on {url} ({exc})")
    print(f"data          {data_root()}")
    print(f"install       {install_root()}")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Advance the schema after a new build has been laid down.

    The installer replaces the install root and then calls this. Migrations are
    the only thing an upgrade has to do that laying down files does not, and
    `migrate` takes its unconditional pre-migration dump first, so a failed
    upgrade leaves a restorable store rather than a half-migrated one.
    """
    del args
    cfg = read_config()
    pg_start(cfg)
    wait_for_pg(cfg)
    _migrate(cfg)
    print("schema is current")
    return 0



def cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the shipped binary is complete, without needing a database.

    A frozen build fails in ways a source run cannot: a module uvicorn names at
    runtime that the analysis could not see, a data file setuptools ships that
    PyInstaller did not, a native library that resolved on the build machine
    because a wheel was on sys.path. Every one of those looks identical from
    outside: a server that starts and then does nothing.

    So this imports the pieces and uses them. It runs in CI on the artifact that
    ships, and it is the first thing to run on a laptop where something is
    wrong, because it separates "the install is broken" from "the database is
    broken" in one command.
    """
    payload = not args.binary_only
    failures: list[str] = []

    def probe(label: str, fn) -> None:
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 -- reporting every failure is the point
            failures.append(f"{label}: {exc!r}")
            print(f"  FAIL  {label}: {exc!r}")
        else:
            print(f"  ok    {label}{': ' + detail if detail else ''}")

    print(f"engraphy-win selftest, install root {install_root()}")

    def _migrations() -> str:
        from engraphy.admin import migrate
        version = migrate.expected_schema_version(migrate.DEFAULT_MIGRATIONS_DIR)
        return f"schema {version}"

    def _pack_schema() -> str:
        import json
        from engraphy.admin import packs
        return f"{len(json.loads(packs.SCHEMA_PATH.read_text(encoding='utf-8')))} top-level keys"

    def _uvicorn() -> str:
        import uvicorn.lifespan.on
        import uvicorn.loops.auto
        import uvicorn.protocols.http.auto
        import uvicorn
        return uvicorn.__version__

    def _psycopg() -> str:
        import psycopg
        import psycopg_pool
        # The pool is named rather than merely imported: an import a linter can
        # call unused is an import a future cleanup deletes, and the pool is
        # exactly the piece a frozen build loses without anything else noticing.
        return (f"{psycopg.__version__} on {psycopg.pq.__impl__}, "
                f"pool {psycopg_pool.__version__}")

    def _embedding() -> str:
        from engraphy.core import embedding
        got = embedding.profile()
        if got != SHIPPED_PROFILE:
            # Not a warning. A different profile means a different model, which
            # means either a network download of something the payload does not
            # carry, or vectors that do not match what the store was written
            # with. Both are failures, and both are silent without this.
            raise RuntimeError(
                f"profile is {got!r}, but this distribution ships {SHIPPED_PROFILE!r}")
        vec = embedding.embed_document("selftest: the model loads and produces a vector")
        if len(vec) != embedding.DIMS:
            raise RuntimeError(f"got {len(vec)} dimensions, expected {embedding.DIMS}")
        return f"{got}, {len(vec)} dimensions, from {os.environ['HF_HOME']}"

    def _postgres() -> str:
        exe = pg_bin("pg_config")
        if not exe.exists():
            raise FileNotFoundError(str(exe))
        r = _run([str(exe), "--version"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()

    def _pgvector() -> str:
        _install_pgvector(install_root(), pathlib.Path("."))
        return "vector.dll and its SQL are present"

    probe("migrations resolve as package data", _migrations)
    probe("pack schema resolves as package data", _pack_schema)
    probe("uvicorn's runtime-named modules", _uvicorn)
    probe("psycopg and its pool", _psycopg)
    probe("the MCP server builds", lambda: __import__(
        "engraphy.server.app", fromlist=["create_app"]) and "engraphy.server.app imported")
    probe("the embedding model", _embedding)
    # The two payload checks come last, and are skippable, because they are
    # about what the INSTALLER laid down rather than about what was frozen. The
    # build that produces the binary has no Postgres beside it yet and skips
    # them; every other caller wants them, because on a real install a missing
    # cluster is the failure rather than a category that does not apply.
    if payload:
        probe("the bundled PostgreSQL", _postgres)
        probe("the pgvector build", _pgvector)
    else:
        print("  skip  the bundled PostgreSQL and pgvector (--binary-only)")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0



# --------------------------------------------------------------------------
# what this distribution is
# --------------------------------------------------------------------------

#: The embedding profile the Windows distribution ships. gte-small int8: the
#: whole stack measured 187MB resident with it against 983MB on the shipped
#: defaults (docs/footprint-2026-09-09.md), and 8GB of RAM is the machine this
#: targets.
SHIPPED_PROFILE = "micro"


def apply_distribution_env() -> None:
    """Point every verb at the model this distribution actually ships.

    Applied for the whole program rather than inside `run`, because a store is
    written under one embedding profile and has to keep being read under it, and
    because the alternative is worse than an inconsistency. With these unset, any
    other verb falls back to the DEFAULT profile and its model is not in the
    payload, so `selftest` on a consultant's laptop would reach for the network
    and download half a gigabyte, or fail outright on a machine that has none.

    `setdefault`, so an operator debugging a specific profile can still override
    either from the environment.
    """
    os.environ.setdefault("ENGRAPHY_EMBEDDING_PROFILE", SHIPPED_PROFILE)
    # The baked cache, laid down beside the binary at build time. Offline and
    # instant on first boot, the same property the Docker image gets from its
    # prebake.
    os.environ.setdefault("HF_HOME", str(install_root() / "model"))


TASK_NAME = "Engraphy"


def main(argv: list[str] | None = None) -> int:
    # `token` calls psycopg's async connect through asyncio.run, and psycopg's
    # async mode refuses Windows' default ProactorEventLoop. Installed for the
    # whole process rather than around that one call, because this program is
    # Windows-only and every async path in it has the same requirement.
    # engraphy/admin/cli.py and engraphy/server/app.py do the same, and
    # scripts/check_windows_event_loop.py guards all three.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    apply_distribution_env()

    parser = argparse.ArgumentParser(prog="engraphy-win", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap", help="first run: cluster, schema, role, space")
    p.add_argument("--pg-port", type=int, default=DEFAULT_PG_PORT)
    p.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    p.add_argument("--space", default=DEFAULT_SPACE)
    p.add_argument("--principal", default=DEFAULT_PRINCIPAL)
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("run", help="start postgres and serve (what the logon task runs)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("stop", help="stop the server and the cluster")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="what is running")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("token", help="mint a client token")
    p.add_argument("--client-name", default="desktop")
    p.add_argument("--write-handoff", action="store_true", default=True)
    p.add_argument("--no-write-handoff", dest="write_handoff", action="store_false")
    p.add_argument("--print-token", action="store_true")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("upgrade", help="migrate the schema after a new build")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("selftest", help="prove the install is complete, no database needed")
    p.add_argument("--binary-only", action="store_true",
                   help="skip the checks that need the bundled PostgreSQL beside the binary")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
