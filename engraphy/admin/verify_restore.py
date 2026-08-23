"""verify-restore --against dump.pgdump: schema version match, per-space row
counts, constraint tests against the restored DB, sentinel retrieval
(design/04 s.Backup contract).

Restores into a scratch database (created and dropped by this tool -- never
the operator's live DB) so a deployment's cron can run this monthly without
touching production data. Requires `pg_restore` on PATH, and a superuser/owner
connection able to CREATE DATABASE / DROP DATABASE.
"""
import contextlib
import pathlib
import shutil
import subprocess
import uuid

import psycopg

from engraphy.admin.migrate import expected_schema_version
from engraphy.core import sentinel

# spaces.id has no FK dependency, so this probe always runs and always
# isolates the regex CHECK it claims to test (spaces_id_check).
_SPACE_ID_PROBE = (
    "spaces.id pattern CHECK",
    "INSERT INTO spaces (id, display_name) VALUES ('Not Valid!!', 'verify-restore probe')",
    psycopg.errors.CheckViolation,
)

# nodes-table probes need a REAL (space_id, type, scope_id) triple from the
# restored data -- an invented one would hit the space_id FK before ever
# reaching the CHECK/trigger under test, proving nothing about that
# constraint. _run_constraint_probes binds %s from a row it queries out of
# the restored DB, and skips these two if the restore has no registry to
# probe against (an edge case for a near-empty dump).
_NODE_BODY_LENGTH_PROBE = (
    "nodes.body length CHECK",
    ("INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
    "embedding_model, source_client, author_principal) "
    "VALUES (%s, %s, %s, 'valid probe title', '', '{}', "
    "array_fill(0, ARRAY[384])::vector, 'test', 'probe', 'probe')"),
    psycopg.errors.CheckViolation,
)
_NODE_STATUS_CANONICAL_PROBE = (
    "nodes.status/canonical_id CHECK",
    ("INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, status, canonical_id, "
    "embedding, embedding_model, source_client, author_principal) "
    "VALUES (%s, %s, %s, 'valid probe title', 'valid probe body', "
    "'{}', 'merged', NULL, array_fill(0, ARRAY[384])::vector, 'test', 'probe', 'probe')"),
    psycopg.errors.CheckViolation,
)


class VerifyRestoreError(Exception):
    """A step in the verify-restore sequence failed; the message names which."""


def _admin_conninfo(conninfo: str, dbname: str) -> str:
    """Swap the database name in a conninfo/URL string. psycopg.conninfo round-trips
    any libpq-accepted form (URL or keyword string) through parse/make, which is
    simpler and less error-prone than string-splicing a URL by hand."""
    parsed = psycopg.conninfo.conninfo_to_dict(conninfo)
    parsed["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**parsed)


@contextlib.contextmanager
def _scratch_database(admin_conninfo: str, name_prefix: str = "engraphy_verify_restore"):
    """Creates a uniquely-named scratch DB on the same server, yields its
    conninfo, and drops it afterward regardless of outcome. `admin_conninfo`
    must point at a DB the connecting role can already reach (postgres/template1
    conventionally) -- CREATE DATABASE cannot run inside a transaction block, so
    this connects with autocommit=True."""
    scratch_name = f"{name_prefix}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(f'CREATE DATABASE "{scratch_name}"')
    try:
        yield _admin_conninfo(admin_conninfo, scratch_name), scratch_name
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as conn:
            cur = conn.cursor()
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)')


def _restore(dump_path: pathlib.Path, scratch_conninfo: str) -> None:
    if shutil.which("pg_restore") is None:
        raise VerifyRestoreError("pg_restore not found on PATH -- install the Postgres client tools")
    result = subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", "--dbname", scratch_conninfo, str(dump_path)],
        capture_output=True, text=True, check=False,
    )
    # pg_restore exits nonzero on ANY warning (e.g. "role does not exist" for
    # ownership it can't reassign, harmless with --no-owner) as well as real
    # failures, so a nonzero code alone isn't decisive -- but a real failure
    # always leaves the target unqueryable, which the caller's next step
    # (schema_migrations check) catches regardless of pg_restore's exit code.
    if result.returncode != 0 and "error" in result.stderr.lower():
        raise VerifyRestoreError(f"pg_restore reported an error: {result.stderr.strip()}")


def _check_schema_version(scratch_conninfo: str, migrations_dir: pathlib.Path) -> str:
    expected = expected_schema_version(migrations_dir)
    with psycopg.connect(scratch_conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT max(version) FROM schema_migrations")
        except psycopg.errors.UndefinedTable as exc:
            raise VerifyRestoreError("restored DB has no schema_migrations table -- restore failed") from exc
        (applied,) = cur.fetchone()
    if applied != expected:
        raise VerifyRestoreError(
            f"restored DB's schema_migrations is at {applied!r}, this install expects {expected!r}")
    return f"schema_migrations at {applied!r} (matches expected)"


def _row_counts(scratch_conninfo: str) -> list[str]:
    with psycopg.connect(scratch_conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM spaces")
        (space_count,) = cur.fetchone()
        if space_count == 0:
            raise VerifyRestoreError("restored DB has zero spaces -- dump looks empty or corrupt")
        cur.execute(
            "SELECT space_id, count(*) FROM nodes GROUP BY space_id ORDER BY space_id")
        rows = cur.fetchall()
    lines = [f"{space_count} space(s) restored"]
    lines.extend(f"  space {space_id!r}: {count} node(s)" for space_id, count in rows)
    return lines


def _run_one_probe(cur, label: str, sql: str, expected_exc: type[Exception], params=()) -> str:
    cur.execute("SAVEPOINT probe")
    try:
        cur.execute(sql, params)
    except expected_exc:
        cur.execute("ROLLBACK TO SAVEPOINT probe")
        return f"constraint probe ok: {label}"
    except psycopg.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT probe")
        raise VerifyRestoreError(
            f"constraint probe {label!r} raised {type(exc).__name__}, expected "
            f"{expected_exc.__name__}") from exc
    else:
        cur.execute("ROLLBACK TO SAVEPOINT probe")
        raise VerifyRestoreError(f"constraint probe {label!r} did not raise -- constraint is not live")


def _run_constraint_probes(scratch_conninfo: str) -> list[str]:
    lines = []
    with psycopg.connect(scratch_conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        label, sql, expected_exc = _SPACE_ID_PROBE
        lines.append(_run_one_probe(cur, label, sql, expected_exc))

        # A real (space, type, scope) triple to probe the nodes-table CHECKs
        # against -- an invented one would trip the space_id FK first
        # instead of the CHECK under test.
        cur.execute(
            "SELECT nt.space_id, nt.name, s.id FROM node_types nt "
            "JOIN scopes s ON s.space_id = nt.space_id LIMIT 1")
        row = cur.fetchone()
        if row is None:
            lines.append("nodes CHECK probes: skipped (restored DB has no registry to probe against)")
        else:
            space_id, type_name, scope_id = row
            for label, sql, expected_exc in (_NODE_BODY_LENGTH_PROBE, _NODE_STATUS_CANONICAL_PROBE):
                lines.append(_run_one_probe(cur, label, sql, expected_exc, (space_id, type_name, scope_id)))

        conn.rollback()  # no probe's effects are ever meant to persist
    return lines


def _sentinel_check(scratch_conninfo: str, sentinel_id: str | None) -> list[str]:
    """Retrieval proof, resolved in the three-step order design/04 s.Backup
    contract specifies:

    1. **Per space, from config `sentinel.node_id`** -- the house convention
       (`space create` mints one sentinel per space and records its id), which is
       what makes the monthly proof self-contained instead of depending on an
       operator remembering to maintain a canary.
    2. **`--sentinel-id`**, for a space that predates the convention or an
       operator who wants a different node checked.
    3. **Skipped, with a log line naming the space.** A pre-convention space is
       not a restore failure, and failing here would make the verb useless on
       exactly the deployments that most need it. The skip is *logged per space*
       so "no sentinel configured" can never be mistaken for "sentinel passed".

    Every configured space is checked, not just the first: a multi-space instance
    where one space restored and another did not is precisely the failure this
    exists to catch. For the engine's own sentinel, content is compared and not
    merely presence -- a row whose title survived but whose body did not is a
    failed restore, and only this node's content is predictable enough to assert.
    An operator-supplied `--sentinel-id` is additionally verified even if every
    space answered from config, so naming an id always means checking it.
    """
    lines: list[str] = []
    used_explicit = False
    with psycopg.connect(scratch_conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT s.id, c.value #>> '{}' FROM spaces s "
            "LEFT JOIN config c ON c.space_id = s.id AND c.key = %s ORDER BY s.id",
            (sentinel.SENTINEL_CONFIG_KEY,),
        )
        configured = cur.fetchall()

        for space_id, configured_id in configured:
            target = configured_id or sentinel_id
            if not target:
                lines.append(
                    f"  space {space_id!r}: sentinel retrieval skipped "
                    f"(no '{sentinel.SENTINEL_CONFIG_KEY}' in config, no --sentinel-id)")
                continue
            source = "config" if configured_id else "--sentinel-id"
            cur.execute("SELECT title, body, status FROM nodes WHERE id = %s", (target,))
            row = cur.fetchone()
            if row is None:
                raise VerifyRestoreError(
                    f"sentinel node {target!r} (from {source}) for space {space_id!r} "
                    f"not found in restored DB")
            title, body, status = row
            if configured_id and (title != sentinel.SENTINEL_TITLE or body != sentinel.SENTINEL_BODY):
                raise VerifyRestoreError(
                    f"sentinel node {target!r} for space {space_id!r} restored with altered "
                    f"content -- title {title!r}, body length {len(body)} "
                    f"(expected {sentinel.SENTINEL_TITLE!r}, length {len(sentinel.SENTINEL_BODY)})")
            # Only the engine's own sentinel has content this code can predict;
            # an operator-supplied canary is any node, so presence is all that
            # can honestly be claimed for it.
            proof = "content matches" if configured_id else "present"
            lines.append(
                f"  space {space_id!r}: sentinel {target!r} retrieved from {source} "
                f"(status {status!r}, {proof})")
            used_explicit = used_explicit or not configured_id

        # An explicitly-supplied --sentinel-id is ALWAYS verified, even when
        # every space resolved its own from config (config wins per space, as
        # design/04 specifies) or when there are no spaces rows at all. The
        # operator named a node; silently not checking it because config
        # happened to answer first would be the one failure mode this verb must
        # not have. run() never sees the no-spaces case (_row_counts fails
        # first), but _sentinel_check is also called directly as a unit.
        if sentinel_id and not used_explicit:
            cur.execute("SELECT title FROM nodes WHERE id = %s", (sentinel_id,))
            if cur.fetchone() is None:
                raise VerifyRestoreError(
                    f"sentinel node {sentinel_id!r} not found in restored DB")
            lines.append(f"  sentinel {sentinel_id!r} retrieved from --sentinel-id "
                         f"(not attributed to a space)")
    return lines


def run(
    dump_path: pathlib.Path,
    admin_conninfo: str,
    *,
    migrations_dir,
    sentinel_id: str | None = None,
) -> list[str]:
    """The full assertion suite against a scratch restore of `dump_path`.
    `admin_conninfo` is a connection the operator role can already reach
    (used to CREATE/DROP the scratch DB); the restore itself and every
    assertion run against the scratch DB, never the live one."""
    if not dump_path.exists():
        raise VerifyRestoreError(f"dump file not found: {dump_path}")

    log: list[str] = []
    with _scratch_database(admin_conninfo) as (scratch_conninfo, scratch_name):
        log.append(f"restoring into scratch database {scratch_name!r}")
        _restore(dump_path, scratch_conninfo)
        log.append(_check_schema_version(scratch_conninfo, migrations_dir))
        log.extend(_row_counts(scratch_conninfo))
        log.extend(_run_constraint_probes(scratch_conninfo))
        log.append("sentinel retrieval:")
        log.extend(_sentinel_check(scratch_conninfo, sentinel_id))
    log.append(f"scratch database {scratch_name!r} dropped")
    return log
