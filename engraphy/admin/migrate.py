"""migrate: unconditional pre-dump -> dbmate up -> restart -> smoke test (design/04
s.Engine migrations).

The dump step has no skip flag by design -- "unconditional" is the safety property
design/04 asks for; an operator who wants to skip it can delete the file afterward,
but the tool never gives them a flag to not take it in the first place.

`--restart-cmd` is a plain shell string the operator supplies (systemd's
`systemctl restart engraphy`, a compose `docker compose restart engraphy`, or
whatever their profile uses) -- this tool doesn't know or assume how the process
is supervised, so it shells out to whatever the operator's install actually needs,
or prints a reminder if the operator didn't give one.
"""
import datetime
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.request

import psycopg

DEFAULT_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"


class MigrateError(Exception):
    """A step in the migrate sequence failed; the message names which step."""


def expected_schema_version(migrations_dir: pathlib.Path) -> str:
    """Mirrors engraphy.server.app.expected_schema_version but against an
    arbitrary migrations dir (an operator may point --migrations-dir at a
    vendored copy rather than this install's own package data)."""
    versions = sorted(p.name.split("_", 1)[0] for p in migrations_dir.glob("*.sql"))
    if not versions:
        raise MigrateError(f"no migration files found in {migrations_dir}")
    return versions[-1]


def pre_dump(conninfo: str, dump_dir: pathlib.Path) -> pathlib.Path:
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = dump_dir / f"pre-migrate-{stamp}.pgdump"
    if shutil.which("pg_dump") is None:
        raise MigrateError("pg_dump not found on PATH -- install the Postgres client tools")
    result = subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(dump_path), conninfo],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise MigrateError(f"pre-dump failed: {result.stderr.strip()}")
    return dump_path


def dbmate_up(conninfo: str, migrations_dir: pathlib.Path) -> str:
    if shutil.which("dbmate") is None:
        raise MigrateError("dbmate not found on PATH -- install it before running migrate")
    result = subprocess.run(
        ["dbmate", "--migrations-dir", str(migrations_dir), "--no-dump-schema", "--url", conninfo, "up"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise MigrateError(f"dbmate up failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


# --- In-process applier (no dbmate binary required) ---------------------------
#
# dbmate is a single-purpose Go binary whose entire job here is: keep a
# `schema_migrations` ledger, and apply each unapplied `-- migrate:up` block in
# its own transaction. Requiring that binary on the host made the pip/local
# install path (and Windows especially) awkward for no engine-behavior reason.
# `apply_migrations` reproduces dbmate's up-path over psycopg so `engraphy-admin
# migrate` needs nothing but the Python deps already pinned in pyproject. It is
# byte-for-byte schema-compatible with `dbmate up` (validated by a schema diff,
# see engraphy/tests/test_migrate_in_process.py): same ledger table, same version
# strings, same per-file transaction boundary. dbmate remains selectable
# (`--use-dbmate`) but is no longer a hard requirement.
#
# The ledger DDL matches what `dbmate up` leaves behind exactly (an unbounded
# `character varying` column with a `schema_migrations_pkey` primary key -- see
# the pg_dump at engraphy/db/schema.sql). Writing `varchar(255)`/`varchar(128)`
# here would make the schema diff fail, so it is deliberately unbounded.
SCHEMA_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
    "version character varying NOT NULL, "
    "CONSTRAINT schema_migrations_pkey PRIMARY KEY (version))"
)


def _up_section(sql_text: str, filename: str) -> str:
    """Extract the SQL between `-- migrate:up` and `-- migrate:down` (or EOF).

    dbmate's file format. The whole up-section is returned as one string and
    executed in a single psycopg call -- NOT split on `;`, because the trigger
    and DO-block migrations (0006, 0017, 0018) carry semicolons inside
    dollar-quoted function bodies that a naive split would shred. psycopg sends a
    multi-statement string in one round trip as long as no parameters are bound,
    which is exactly this case.

    Line endings are PRESERVED verbatim (splitlines(keepends=True), joined with
    ''): a CRLF checkout on Windows must execute CRLF-terminated bodies, exactly
    as `dbmate up` does from the same file, so the function source Postgres
    stores in pg_proc.prosrc is byte-for-byte what dbmate would store on that
    same checkout. Normalizing to LF here would make the schema diff against
    dbmate show every trigger/function body as changed (line endings only) even
    though the SQL is semantically identical -- the whole point of this applier
    is to match dbmate's schema exactly, so it must not rewrite the bytes it
    runs.
    """
    lines = sql_text.splitlines(keepends=True)
    up_idx = down_idx = None
    up_options = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if up_idx is None and stripped.startswith("-- migrate:up"):
            up_idx = i
            up_options = stripped[len("-- migrate:up"):].strip()
        elif up_idx is not None and stripped.startswith("-- migrate:down"):
            down_idx = i
            break
    if up_idx is None:
        raise MigrateError(f"{filename}: no '-- migrate:up' marker found")
    if "transaction:false" in up_options:
        # None of the shipped migrations use this (each is transactional), and
        # the in-process applier deliberately wraps every migration in one
        # transaction. Refuse loudly rather than silently run a
        # non-transactional migration inside a transaction (e.g. a future
        # CREATE INDEX CONCURRENTLY), which would error opaquely. Such a
        # migration must go through `--use-dbmate`.
        raise MigrateError(
            f"{filename}: '-- migrate:up transaction:false' is not supported by the "
            f"in-process applier; run with --use-dbmate for this migration")
    end = down_idx if down_idx is not None else len(lines)
    # Join without a separator (keepends kept each line's own terminator); strip
    # only outer whitespace, which lives between statements and is never stored
    # in any object body -- interior line endings are untouched.
    return "".join(lines[up_idx + 1:end]).strip()


def apply_migrations(conninfo: str, migrations_dir: pathlib.Path) -> list[str]:
    """Apply every unapplied migration in `migrations_dir`, in filename order,
    each in its own transaction alongside its `schema_migrations` ledger insert
    (dbmate's atomicity guarantee). Idempotent: versions already in the ledger
    are skipped, so a re-run applies exactly the remainder. Returns the list of
    versions applied by THIS call (empty if already up to date)."""
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise MigrateError(f"no migration files found in {migrations_dir}")

    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_MIGRATIONS_DDL)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM public.schema_migrations")
            already = {row[0] for row in cur.fetchall()}

        for path in files:
            version = path.name.split("_", 1)[0]
            if version in already:
                continue
            body = _up_section(path.read_text(encoding="utf-8"), path.name)
            try:
                with conn.cursor() as cur:
                    if body:
                        cur.execute(body)  # no params -> multi-statement string is sent as-is
                    cur.execute(
                        "INSERT INTO public.schema_migrations (version) VALUES (%s)", (version,))
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                raise MigrateError(
                    f"migration {path.name} failed: {str(exc).strip()}") from exc
            applied.append(version)
    return applied


def restart(restart_cmd: str | None) -> str:
    if not restart_cmd:
        return ("no --restart-cmd given -- restart the engraphy process manually "
                "(systemctl restart engraphy / docker compose restart engraphy / equivalent)")
    result = subprocess.run(restart_cmd, shell=True, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MigrateError(f"restart command failed: {result.stderr.strip() or result.stdout.strip()}")
    return f"ran restart command: {restart_cmd!r}"


def smoke_test(conninfo: str, migrations_dir: pathlib.Path, healthz_url: str | None) -> list[str]:
    """Post-restart smoke test: the DB is reachable, schema_migrations now
    matches this install's expectation (the same check app.py's boot gate
    makes), and spaces is queryable. Optionally GETs --healthz-url too, if the
    operator's restart brought the process back up somewhere reachable."""
    lines: list[str] = []
    expected = expected_schema_version(migrations_dir)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT max(version) FROM schema_migrations")
        except psycopg.errors.UndefinedTable as exc:
            raise MigrateError(
                "smoke test: schema_migrations does not exist after `dbmate up` -- "
                "migrate did not actually run against this DB") from exc
        (applied,) = cur.fetchone()
        if applied != expected:
            raise MigrateError(
                f"smoke test: schema_migrations is at {applied!r} after migrate, expected {expected!r}")
        lines.append(f"schema_migrations at {applied!r} (matches expected)")
        cur.execute("SELECT count(*) FROM spaces")
        (space_count,) = cur.fetchone()
        lines.append(f"spaces table reachable ({space_count} row(s))")

    if healthz_url:
        try:
            with urllib.request.urlopen(healthz_url, timeout=10) as resp:
                status = resp.status
        except urllib.error.URLError as exc:
            raise MigrateError(f"smoke test: GET {healthz_url} failed: {exc}")
        if status != 200:
            raise MigrateError(f"smoke test: GET {healthz_url} returned {status}")
        lines.append(f"GET {healthz_url} -> 200")

    return lines


def run(
    conninfo: str,
    *,
    migrations_dir: pathlib.Path = DEFAULT_MIGRATIONS_DIR,
    dump_dir: pathlib.Path = pathlib.Path("./backups"),
    restart_cmd: str | None = None,
    healthz_url: str | None = None,
    use_dbmate: bool = False,
) -> dict:
    """The full sequence, in order, raising MigrateError with the failing
    step's message on the first failure (later steps never run).

    The migrate-up step runs in-process by default (no external binary); pass
    `use_dbmate=True` to shell out to the `dbmate` binary instead. Either way the
    pre-dump ahead of it is unconditional -- there is no skip flag for it (see the
    module docstring), and that safety property is independent of which applier
    runs."""
    log: list[str] = []

    dump_path = pre_dump(conninfo, dump_dir)
    log.append(f"pre-dump written: {dump_path}")

    if use_dbmate:
        dbmate_output = dbmate_up(conninfo, migrations_dir)
        log.append(f"dbmate up: {dbmate_output or '(already up to date)'}")
    else:
        applied = apply_migrations(conninfo, migrations_dir)
        log.append(
            "migrate up: " + (f"applied {', '.join(applied)}" if applied else "(already up to date)"))

    log.append(restart(restart_cmd))

    log.extend(smoke_test(conninfo, migrations_dir, healthz_url))

    return {"dump_path": str(dump_path), "log": log}
