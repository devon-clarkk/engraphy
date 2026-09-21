"""engraphy-admin: the local instance-operator CLI (design/06 §Space
administration, row 1 -- "Instance operator | engraphy-admin local CLI only").

This surface is deliberately NOT reachable over the network: it runs against the
instance's own DB with an operator-privileged connection (ENGRAPHY_DATABASE_URL, a
superuser/owner role that bypasses RLS), which is what lets it bootstrap a space
before any principal or token exists. The network-reachable subset of space
administration -- add members, mint/revoke tokens, set visibility, manage grants
-- is the space_admin MCP tools (engraphy/server/tools/admin.py); this CLI is the
operator's out-of-band equivalent plus the bootstrap (`space create`) that has to
happen before any token can exist.

Implemented here: `space create`, `space export`, `space import`, `principal add`, `principal archive`,
`principal unarchive`, `token create`,
`token revoke`, `config set`, `import` (JSONL bulk load through the write
pipeline), and `pack validate` / `pack apply`. Still open: `purge-session` (its
addenda-handling is a deferred design decision, E2-plan §5.6) and the E3 verbs
(`migrate` / `verify-restore` / `doctor` / `pack upgrade`), which live in their
own modules.

Personal scopes (design/06 §Personal scopes): every principal CREATED VIA THIS
CLI gets `personal-<id>` (private, ambient, owned by them) in the same
transaction -- the "member-creation makes a personal scope" rule. The MCP
`admin_member_add` deliberately does NOT (DECISIONS-DELTA 3a): a member added
over the network creates their own scopes. So the personal-scope guarantee lives
exactly here, on the privileged path.
"""
import asyncio
import json
import os
import pathlib
import sys

import psycopg
import typer
from psycopg_pool import AsyncConnectionPool

from engraphy.admin import doctor, migrate, packs, verify_restore
from engraphy.admin.export_ import ExportError, export_space
from engraphy.admin.addenda import promote_addenda
from engraphy.admin.import_ import ImportLineError, run_import
from engraphy.admin.reembed import reembed_space
from engraphy.admin.space_import import SpaceImportError, run_space_import
from engraphy.admin.surface import rebuild_surface
from engraphy.core import embedding, sentinel
from engraphy.server.auth import mint_token

if sys.platform == "win32":
    # psycopg's async mode explicitly rejects Windows' default
    # ProactorEventLoop. BOTH async verbs in this CLI go through asyncio.run()
    # and therefore hit it: `token create` (auth.mint_token) and `import`
    # (import_.run_import, which drives the async write pipeline). Without this
    # the operator gets "Psycopg cannot use the 'ProactorEventLoop' to run in
    # async mode" and cannot mint a token at all -- i.e. cannot onboard any MCP
    # client. Mirrors engraphy/tests/conftest.py and engraphy/tests/bench.py, which
    # already install the same policy for the same reason.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = typer.Typer(help="Engraphy instance-operator admin CLI.", no_args_is_help=True)
space_app = typer.Typer(help="Create spaces and their founding principal.", no_args_is_help=True)
principal_app = typer.Typer(
    help="Add, archive and restore principals (members) in a space.", no_args_is_help=True)
token_app = typer.Typer(help="Mint and revoke bearer tokens.", no_args_is_help=True)
config_app = typer.Typer(help="Set per-space config values.", no_args_is_help=True)
pack_app = typer.Typer(help="Validate and apply pack files.", no_args_is_help=True)
addenda_app = typer.Typer(
    help="Recover facts buried as get-only addenda (Phase B).", no_args_is_help=True)
surface_app = typer.Typer(
    help="Rebuild the searchable attr surface (Phase C).", no_args_is_help=True)
app.add_typer(space_app, name="space")
app.add_typer(principal_app, name="principal")
app.add_typer(token_app, name="token")
app.add_typer(config_app, name="config")
app.add_typer(pack_app, name="pack")
app.add_typer(addenda_app, name="addenda")
app.add_typer(surface_app, name="surface")


def _conninfo(database_url: str | None) -> str:
    conninfo = database_url or os.environ.get("ENGRAPHY_DATABASE_URL")
    if not conninfo:
        raise typer.BadParameter("pass --database-url or set ENGRAPHY_DATABASE_URL")
    return conninfo


def _mint_sentinel(cur, space_id: str, principal_id: str) -> str:
    """Register the reserved `engraphy_sentinel` node type, mint the space's
    sentinel node into the founding principal's personal scope, and record its
    id in config under `sentinel.node_id` (design/04 s.Backup contract).

    Runs inside `space create`'s transaction, on the operator's privileged
    connection, so no RLS gate applies and the space either gets a sentinel or
    does not get created at all -- there is deliberately no path that produces a
    sentinel-less space, because a space that quietly lacks one degrades
    verify-restore to a skip without ever saying so.

    The embedding is written as a pgvector literal from
    `sentinel.constant_unit_vector()`: this verb must never load the embedding
    model (see engraphy/core/sentinel.py). `attrs` is left `{}`, which the validate
    trigger accepts against SENTINEL_ATTR_SPEC: that spec declares no keys and
    omits `closed`, and BOTH interpreters default `closed` to **true**
    (`engraphy_validate_attrs`'s COALESCE, `attr_spec.validate_attrs`'s
    `.get("closed", True)`) -- so the sentinel's spec is a closed one with an
    empty key set, and empty attrs satisfy it. That is the stricter reading and
    the desirable one: nothing can ever add an attr to this node. Returns the
    new node's id.
    """
    cur.execute(
        "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, %s, %s, %s)",
        (space_id, sentinel.SENTINEL_NODE_TYPE, sentinel.SENTINEL_TYPE_DESCRIPTION,
         psycopg.types.json.Jsonb(sentinel.SENTINEL_ATTR_SPEC)),
    )
    cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, status, "
        "embedding, embedding_model, source_client, author_principal) "
        "VALUES (%s, %s, %s, %s, %s, '{}', 'archived', %s::vector, %s, 'engraphy-admin', %s) "
        "RETURNING id",
        (space_id, sentinel.SENTINEL_NODE_TYPE, f"personal-{principal_id}",
         sentinel.SENTINEL_TITLE, sentinel.SENTINEL_BODY, sentinel.vector_literal(),
         sentinel.SENTINEL_EMBEDDING_MODEL, principal_id),
    )
    (node_id,) = cur.fetchone()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s)",
        (space_id, sentinel.SENTINEL_CONFIG_KEY, psycopg.types.json.Jsonb(str(node_id))),
    )
    return str(node_id)


def _create_principal_with_personal_scope(cur, space_id: str, principal_id: str,
                                          display_name: str, role: str) -> None:
    """Insert a principal AND their `personal-<id>` scope (private, ambient,
    owned by them) -- design/06's member-creation rule, honored on every
    CLI-created principal. Runs on the operator's privileged connection, so no
    RLS gate applies; the caller owns the surrounding transaction/commit."""
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name, role) VALUES (%s, %s, %s, %s)",
        (space_id, principal_id, display_name, role),
    )
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, description, owner_principal, visibility, ambient) "
        "VALUES (%s, %s, %s, %s, %s, 'private', true)",
        (
            space_id, f"personal-{principal_id}", f"{display_name}'s personal memory",
            # description (migration 0022): give the founding personal scope real
            # routing text so scope_guide is complete for a brand-new space.
            (f"{display_name}'s private memory: personal facts, preferences, people, and "
            "commitments about them. Write here for anything personal to this user rather "
            "than a shared project or team scope."),
            principal_id,
        ),
    )


@space_app.command("create")
def space_create(
    id: str = typer.Option(..., help="Space id (^[a-z0-9][a-z0-9-]{1,62}$)."),
    display_name: str = typer.Option(..., "--display-name", help="Human-readable space name."),
    principal: str = typer.Option(..., help="Founding principal id (becomes a space_admin)."),
    principal_display_name: str = typer.Option(
        None, "--principal-display-name", help="Founding principal's display name (defaults to its id)."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Create a space, its founding principal (role space_admin, so the space is
    administrable via the MCP admin tools from the start), and that principal's
    personal scope -- all in one transaction."""
    conninfo = _conninfo(database_url)
    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (id, display_name))
            _create_principal_with_personal_scope(
                cur, id, principal, principal_display_name or principal, "space_admin")
            sentinel_id = _mint_sentinel(cur, id, principal)
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            raise typer.BadParameter(f"space '{id}' or principal '{principal}' already exists")
        except psycopg.Error as exc:
            conn.rollback()
            raise typer.BadParameter(str(exc).strip())
    typer.echo(f"created space '{id}' with founding space_admin '{principal}' "
               f"and scope 'personal-{principal}'")
    typer.echo(f"minted restore sentinel {sentinel_id} (config '{sentinel.SENTINEL_CONFIG_KEY}')")


# Module-level: a list-typed option default must not be a call in the signature (B008).
_EXPORT_SCOPE_OPTION = typer.Option(
    None, "--scope", help="Export only this scope id. Repeat for several; default all.")
_SCOPE_MAP_OPTION = typer.Option(
    None, "--scope-map", help="Map a source scope to a destination scope, SRC=DST. Repeatable.")


@space_app.command("export")
def space_export(
    space: str = typer.Option(..., help="Space id to export."),
    out: str = typer.Option(..., "--out", help="Path of the JSONL bundle to write."),
    scope: list[str] = _EXPORT_SCOPE_OPTION,
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Write a space's scopes, node types, nodes and edges to a JSONL bundle for
    `space import` on another engine. Read-only: the connection refuses writes, so
    exporting a live engine changes nothing in it."""
    try:
        counts = export_space(_conninfo(database_url), space, out, scopes=scope or None)
    except ExportError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"exported space '{space}' to {out}: {counts['scopes']} scopes, "
               f"{counts['node_types']} node types, {counts['nodes']} nodes, {counts['edges']} edges")


@space_app.command("import")
def space_import(
    file: str = typer.Argument(..., help="Bundle written by `space export`."),
    space: str = typer.Option(..., help="Destination space id on this engine."),
    principal: str = typer.Option(..., help="Destination principal: authors every imported node."),
    scope_map: list[str] = _SCOPE_MAP_OPTION,
    create_scopes: bool = typer.Option(
        True, "--create-scopes/--no-create-scopes",
        help="Create destination scopes that do not exist (default), or refuse."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run the prechecks and print the scope plan; write nothing."),
    review: str = typer.Option(
        None, "--review", help="Path for the review CSV (default: alongside FILE)."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Replay a `space export` bundle into a space on this engine, under a
    principal of this engine. Nodes go through the write pipeline and edges
    through link, so the destination's space, principal, scopes, attr specs,
    embedding model and dedup all apply. Re-running the same bundle writes
    nothing new."""
    conninfo = _conninfo(database_url)

    async def _run():
        pool = AsyncConnectionPool(conninfo, open=False)
        await pool.open()
        try:
            return await run_space_import(
                pool, conninfo, space, principal, file, scope_map=scope_map or None,
                create_scopes=create_scopes, dry_run=dry_run, review_path=review)
        finally:
            await pool.close()

    try:
        summary = asyncio.run(_run())
    except (SpaceImportError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc))
    d = summary.as_dict()
    typer.echo("scope map: " + ", ".join(f"{k} -> {v}" for k, v in sorted(d["scope_map"].items())))
    if d["scopes_created"]:
        verb = "would create" if dry_run else "created"
        typer.echo(f"{verb} scopes: {', '.join(d['scopes_created'])}")
    if dry_run:
        typer.echo(f"dry run: prechecks passed for {d['nodes_total']} nodes and "
                   f"{d['edges_total']} edges; nothing written")
        return
    typer.echo(
        f"nodes: {d['nodes_total']} in bundle; {d['inserted']} inserted, "
        f"{d['already_present']} already present, {d['merged']} merged, "
        f"{d['merged_linked']} merge-linked, {d['resolved_distinct']} resolved distinct, "
        f"{d['left_for_review']} left for review, {d['merged_status_skipped']} merged-status skipped, "
        f"{d['skipped_archived_scope']} skipped in an archived scope")
    typer.echo(
        f"edges: {d['edges_total']} in bundle; {d['edges_attached']} attached, "
        f"{d['edges_already_present']} already present, {d['edges_skipped_unmapped']} unmapped, "
        f"{d['edges_skipped_same_node']} same node, {d['edges_skipped_no_rule']} no rule, "
        f"{d['edges_failed']} failed")
    typer.echo(f"restored: {d['statuses_restored']} statuses, "
               f"{d['reserved_attrs_restored']} nodes' reserved attrs")
    if d["review_path"]:
        typer.echo(f"review: {d['review_path']} lists nodes parked for resolve_duplicate; "
                   f"re-run the import after resolving them to attach their edges")


@principal_app.command("add")
def principal_add(
    space: str = typer.Option(..., help="Space id."),
    id: str = typer.Option(..., help="New principal id."),
    display_name: str = typer.Option(..., "--display-name", help="Principal's display name."),
    role: str = typer.Option("member", help="member | space_admin."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Add a principal to an existing space, with their personal scope."""
    conninfo = _conninfo(database_url)
    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        try:
            _create_principal_with_personal_scope(cur, space, id, display_name, role)
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            raise typer.BadParameter(f"principal '{id}' already exists in space '{space}'")
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback()
            raise typer.BadParameter(f"space '{space}' does not exist")
        except psycopg.Error as exc:
            conn.rollback()
            raise typer.BadParameter(str(exc).strip())
    typer.echo(f"added principal '{id}' ({role}) to space '{space}' with scope 'personal-{id}'")


def _set_archived(conninfo: str, space: str, principal_id: str, archived: bool) -> bool:
    """Flip principals.archived to `archived` for one principal. Returns whether
    the row changed (False when it already held that value); raises
    BadParameter when the principal does not exist in the space."""
    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE principals SET archived = %s "
            "WHERE space_id = %s AND id = %s AND archived <> %s",
            (archived, space, principal_id, archived),
        )
        changed = cur.rowcount == 1
        cur.execute("SELECT 1 FROM principals WHERE space_id = %s AND id = %s", (space, principal_id))
        exists = cur.fetchone() is not None
        conn.commit()
    if not exists:
        raise typer.BadParameter(f"principal '{principal_id}' does not exist in space '{space}'")
    return changed


@principal_app.command("archive")
def principal_archive(
    space: str = typer.Option(..., help="Space id."),
    id: str = typer.Option(..., help="Principal id to archive."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Offboard a principal: every token it holds is refused (401) from its next
    request, the same as a revoked token, with no cache window. The principal's
    row, scopes and nodes stay in place (no hard deletes, design/01), so the
    provenance on everything it wrote is kept. The server enforces the flag in
    auth.require_active_principal (design/03 lists `principal add|archive`).
    `principal unarchive` reverses it."""
    if not _set_archived(_conninfo(database_url), space, id, True):
        typer.echo(f"principal '{id}' in space '{space}' is already archived")
        return
    typer.echo(f"archived principal '{id}' in space '{space}'; its tokens are refused from the next request")


@principal_app.command("unarchive")
def principal_unarchive(
    space: str = typer.Option(..., help="Space id."),
    id: str = typer.Option(..., help="Principal id to restore."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Restore an archived principal: its tokens that are not revoked
    authenticate again from their next request, with no cache window."""
    if not _set_archived(_conninfo(database_url), space, id, False):
        typer.echo(f"principal '{id}' in space '{space}' is not archived")
        return
    typer.echo(f"restored principal '{id}' in space '{space}'; its live tokens authenticate from the next request")


@token_app.command("create")
def token_create(
    space: str = typer.Option(..., help="Space id."),
    principal: str = typer.Option(..., help="Principal the token authenticates as."),
    client_name: str = typer.Option(..., "--client-name", help="Client/device name (provenance)."),
    role: str = typer.Option("readwrite", help="readwrite | readonly."),
    no_scope_all: bool = typer.Option(
        False, "--no-scope-all",
        help="Refuse scope='all' to this token (search/briefing raise ENGRAPHY_SCOPE). "
             "For unattended sessions: they may read any scope they NAME, never "
             "every scope at once."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Mint a display-once bearer token. The plaintext is printed exactly once,
    here, and never recoverable -- only its SHA-256 is stored. Reuses the same
    auth.mint_token path the admin_token_create MCP tool uses.

    --no-scope-all is the per-token scope restriction (migration 0023). It is
    set at mint time and never changed afterwards: re-mint to change your mind.
    Without it, the token is unrestricted, which is what every token minted
    before this flag existed already is."""
    conninfo = _conninfo(database_url)

    async def _run() -> str:
        async with await psycopg.AsyncConnection.connect(conninfo) as aconn:
            # A token for an archived principal would be refused at the door on
            # every request (auth.require_active_principal), so refuse the mint.
            cur = aconn.cursor()
            await cur.execute(
                "SELECT archived FROM principals WHERE space_id = %s AND id = %s", (space, principal))
            row = await cur.fetchone()
            if row is not None and row[0]:
                raise typer.BadParameter(
                    f"principal '{principal}' is archived; run `principal unarchive` before minting")
            raw, _meta = await mint_token(
                aconn, space, principal, client_name, role, no_scope_all=no_scope_all)
            await aconn.commit()
            return raw

    try:
        raw = asyncio.run(_run())
    except psycopg.errors.UniqueViolation:
        raise typer.BadParameter(
            f"a token for principal '{principal}' client '{client_name}' already exists")
    except psycopg.errors.ForeignKeyViolation:
        raise typer.BadParameter(f"principal '{principal}' does not exist in space '{space}'")
    except psycopg.Error as exc:
        raise typer.BadParameter(str(exc).strip())
    if no_scope_all:
        typer.echo("minted WITH --no-scope-all: this token is refused scope='all'.")
    typer.echo("token (shown once -- store it now, it cannot be retrieved later):")
    typer.echo(raw)


@token_app.command("revoke")
def token_revoke(
    space: str = typer.Option(..., help="Space id."),
    principal: str = typer.Option(..., help="Principal the token belongs to."),
    client_name: str = typer.Option(..., "--client-name", help="Client/device name of the token."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Revoke a token by (space, principal, client) -- takes effect on the next
    request (resolve_token re-reads the row every time, no cache window)."""
    conninfo = _conninfo(database_url)
    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE api_tokens SET revoked = true "
            "WHERE space_id = %s AND principal = %s AND client_name = %s AND revoked = false",
            (space, principal, client_name),
        )
        revoked = cur.rowcount
        conn.commit()
    if revoked == 0:
        raise typer.BadParameter(
            f"no active token for principal '{principal}' client '{client_name}' in space '{space}'")
    typer.echo(f"revoked token for principal '{principal}' client '{client_name}'")


@config_app.command("set")
def config_set(
    space: str = typer.Option(..., help="Space id."),
    key: str = typer.Option(..., help="Config key, e.g. space_admin_tools."),
    value: str = typer.Option(..., help="JSON value, e.g. false or 120."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Upsert a per-space config value. `value` is parsed as JSON (so `false`,
    `120`, or a quoted string) and stored in config.value (jsonb). Setting
    `space_admin_tools` to `false` makes the four admin_* tools vanish from that
    space's tools/list entirely."""
    conninfo = _conninfo(database_url)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise typer.BadParameter(f"--value must be valid JSON (got {value!r}); quote strings as '\"x\"'")
    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (space_id, key) DO UPDATE SET value = EXCLUDED.value",
                (space, key, psycopg.types.json.Jsonb(parsed)),
            )
            conn.commit()
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback()
            raise typer.BadParameter(f"space '{space}' does not exist")
    typer.echo(f"set config[{key}] = {value} for space '{space}'")


@app.command("import")
def import_jsonl(
    file: str = typer.Argument(..., help="Path to the JSONL batch to import."),
    space: str = typer.Option(..., help="Space id."),
    scope: str = typer.Option(..., help="Target scope id (must be writable by --principal)."),
    principal: str = typer.Option(..., help="Author principal for every imported node."),
    review_queue: str = typer.Option(
        None, "--review-queue", help="Path for the review-queue CSV (default: alongside FILE)."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Bulk-load a JSONL batch through the standard write pipeline in import mode
    (design/02 §Bulk import). Idempotent by construction: re-running the same file
    is a no-op (every item re-hits >=0.95 against the node it created and absorbs
    silently). PENDING-band items are appended to the review-queue CSV rather than
    parked. Loads the real embedding model, so a large file takes a while."""
    conninfo = _conninfo(database_url)

    async def _run():
        pool = AsyncConnectionPool(conninfo, open=False)
        await pool.open()
        try:
            return await run_import(pool, space, principal, scope, file,
                                    review_queue_path=review_queue)
        finally:
            await pool.close()

    try:
        summary = asyncio.run(_run())
    except (ImportLineError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc))
    d = summary.as_dict()
    typer.echo(
        f"import complete: {d['total']} items "
        f"({d['inserted']} inserted, {d['merged']} merged, "
        f"{d['merged_linked']} merge-linked, {d['review_queued']} review-queued)")
    if d["merged_addendum_dropped"]:
        # Printed only when non-zero, and worded as a warning rather than a
        # statistic: this is the one outcome that discarded caller text with
        # nobody in the loop (ruled 2026-07-21). A near-verbatim correction in
        # the export is exactly what lands here.
        typer.echo(
            f"WARNING: {d['merged_addendum_dropped']} merged item(s) added no addendum -- "
            f"their text was judged a restatement and discarded. If the export contained "
            f"corrections or negations, re-check them: auto-merge cannot tell a correction "
            f"from a restatement, and nothing here superseded anything.")
    if d["review_queue_path"]:
        typer.echo(f"review queue: {d['review_queue_path']}")


@pack_app.command("validate")
def pack_validate(
    file: str = typer.Argument(..., help="Path to the pack YAML/JSON file."),
) -> None:
    """Validate a pack file against packs/schema.json plus the cross-checks
    `validate()` layers on top. Prints each error and exits nonzero if any;
    touches no database."""
    try:
        pack = packs.load_pack_file(file)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc))
    errors = packs.validate(pack)
    if errors:
        for err in errors:
            typer.echo(f"  - {err}")
        raise typer.Exit(1)
    same_topic_warning = packs.check_same_topic_declared(pack)
    if same_topic_warning:
        typer.echo(f"warning: {same_topic_warning}")
    typer.echo(f"pack '{file}' is valid")


@pack_app.command("apply")
def pack_apply(
    file: str = typer.Argument(..., help="Path to the pack YAML/JSON file."),
    space: str = typer.Option(..., help="Space id to apply the pack into."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Validate then apply a pack into an existing space (its node/edge types,
    rules, scopes, briefing/alias/description config). Fails cleanly if the pack
    is invalid, the space is missing, or the pack was already applied (pack
    upgrade is a distinct, not-yet-built path)."""
    conninfo = _conninfo(database_url)
    try:
        pack = packs.load_pack_file(file)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc))
    errors = packs.validate(pack)
    if errors:
        for err in errors:
            typer.echo(f"  - {err}")
        raise typer.BadParameter(f"pack '{file}' is invalid; not applied")
    format_warning = packs.check_pack_format(pack)
    if format_warning:
        typer.echo(f"warning: {format_warning}")
    same_topic_warning = packs.check_same_topic_declared(pack)
    if same_topic_warning:
        typer.echo(f"warning: {same_topic_warning}")

    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        try:
            packs.apply(pack, space, cur)
            conn.commit()
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback()
            raise typer.BadParameter(f"space '{space}' does not exist")
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            raise typer.BadParameter(
                f"pack already applied to space '{space}' (upgrade is not yet supported)")
        except psycopg.Error as exc:
            conn.rollback()
            raise typer.BadParameter(str(exc).strip())
    typer.echo(f"applied pack '{file}' to space '{space}'")


@addenda_app.command("promote")
def addenda_promote_cmd(
    space: str = typer.Option(..., help="Space id to scan."),
    scope: str = typer.Option(None, help="Limit to one scope id (default: all scopes)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be promoted; write nothing."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Promote facts buried as get-only addenda into their own searchable member
    nodes, same_topic-linked to their canonical (fact-searchability-model.md §2.6).
    Idempotent: a promoted addendum is marked and skipped on re-run, so a re-run
    after any crash completes exactly the remainder. Loads the embedding model, so
    a large store takes a while."""
    conninfo = _conninfo(database_url)
    with psycopg.connect(conninfo, autocommit=False) as conn:
        summary = promote_addenda(conn, space, scope, dry_run=dry_run)
    typer.echo(summary.as_line())


@surface_app.command("rebuild")
def surface_rebuild_cmd(
    space: str = typer.Option(..., help="Space id to rebuild."),
    scope: str = typer.Option(None, help="Limit to one scope id (default: all scopes)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be re-embedded; write nothing."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Recompute nodes.extra_search from each node's attrs and re-embed the rows
    whose render changed (fact-searchability-phase-c.md §4). Idempotent: a re-run
    skips rows whose render is unchanged, so a run after a crash completes exactly
    the remainder. Run this immediately after applying the Phase C migration on a
    live space (and after flipping write.attr_surface) -- the flag takes effect on
    existing rows only through a rebuild. Loads the embedding model, so a large
    store takes a while; restore-tested backup first on live spaces (design/04)."""
    conninfo = _conninfo(database_url)
    with psycopg.connect(conninfo, autocommit=False) as conn:
        summary = rebuild_surface(conn, space, scope, dry_run=dry_run)
    typer.echo(summary.as_line())


@app.command("reembed")
def reembed_cmd(
    space: str = typer.Option(..., help="Space id to re-embed."),
    scope: str = typer.Option(None, help="Limit to one scope id (default: all scopes)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be re-embedded; write nothing."),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the confirmation prompt (for scripted upgrades)."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Rewrite stored vectors into the active embedding profile's vector space.

    Needed once, when a store moves onto a profile whose vectors are not
    interchangeable with the ones already written: `onnx-int8` or `micro`. Moving
    between `legacy-torch` and `onnx-fp32` needs nothing, and this command will
    say so and exit, because those two produce interchangeable vectors and share
    a stamp.

    Until it completes the store is part converted and the write path is banding
    across two vector spaces. On `onnx-int8` the error direction is the safe one
    (a near-duplicate opens a confirm round-trip rather than merging silently)
    because it is the same model quantized. On `micro` it is a different model
    and there is no safe direction, so run that one in a quiet window. Either
    way, run to completion rather than leaving a store half done.

    Resumable and idempotent: selection is `embedding_model <> ` the target stamp,
    written in the same statement as the vector, so a killed run resumes where it
    stopped and a completed run finds nothing. Restore-tested backup first on live
    spaces (design/04)."""
    conninfo = _conninfo(database_url)
    target = embedding.MODEL_STAMP
    typer.echo(f"active embedding profile: {embedding.profile()}")
    typer.echo(f"target stamp: {target}")

    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM nodes WHERE space_id = %s AND type <> %s "
            "AND (%s::text IS NULL OR scope_id = %s) AND embedding_model <> %s",
            (space, sentinel.SENTINEL_NODE_TYPE, scope, scope, target))
        pending = cur.fetchone()[0]
        conn.commit()

        if pending == 0:
            typer.echo("nothing to do: every row is already in this vector space.")
            raise typer.Exit(0)

        if not dry_run and not yes:
            typer.confirm(
                f"re-embed {pending} node(s) in space {space!r} into {target}?", abort=True)

        def _progress(done, scanned):
            typer.echo(f"  {done}/{pending} re-embedded ...")

        summary = reembed_space(
            conn, space, scope, dry_run=dry_run, progress=_progress if not dry_run else None)
    typer.echo(summary.as_line())


@app.command("migrate")
def migrate_cmd(
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
    migrations_dir: str = typer.Option(
        None, "--migrations-dir", help="Defaults to the migrations shipped with this install."),
    dump_dir: str = typer.Option(
        "./backups", "--dump-dir", help="Directory for the unconditional pre-migration dump."),
    restart_cmd: str = typer.Option(
        None, "--restart-cmd",
        help="Shell command to restart the engraphy service after migrating "
             "(e.g. 'systemctl restart engraphy'). Printed as a manual reminder if omitted."),
    healthz_url: str = typer.Option(
        None, "--healthz-url", help="If given, GET this URL as part of the post-restart smoke test."),
    use_dbmate: bool = typer.Option(
        False, "--use-dbmate",
        help="Apply migrations by shelling out to the external `dbmate` binary "
             "instead of the built-in in-process applier. The in-process applier "
             "(default) produces the identical schema and needs no extra binary."),
) -> None:
    """Unconditional pre-dump -> migrate up -> restart -> smoke test (design/04
    s.Engine migrations). The pre-dump has no skip flag; it always runs. Migrate
    up runs in-process by default (no `dbmate` binary required); `--use-dbmate`
    selects the external binary instead."""
    conninfo = _conninfo(database_url)
    migrations_path = pathlib.Path(migrations_dir) if migrations_dir else migrate.DEFAULT_MIGRATIONS_DIR
    try:
        result = migrate.run(
            conninfo, migrations_dir=migrations_path, dump_dir=pathlib.Path(dump_dir),
            restart_cmd=restart_cmd, healthz_url=healthz_url, use_dbmate=use_dbmate)
    except migrate.MigrateError as exc:
        raise typer.BadParameter(str(exc))
    for line in result["log"]:
        typer.echo(line)


@app.command("verify-restore")
def verify_restore_cmd(
    against: str = typer.Option(..., "--against", help="Path to the pg_dump (--format=custom) file to verify."),
    database_url: str = typer.Option(
        None, "--database-url",
        help="Overrides ENGRAPHY_DATABASE_URL. Used only to create/drop the scratch DB "
             "this verb restores into -- never touches the live DB."),
    migrations_dir: str = typer.Option(
        None, "--migrations-dir", help="Defaults to the migrations shipped with this install."),
    sentinel_id: str = typer.Option(
        None, "--sentinel-id",
        help="A node id known to be in the dump; its retrieval from the restored DB is asserted. "
             "Skipped (not failed) if omitted."),
) -> None:
    """Restore-test assertion suite against a scratch database: schema version
    match, per-space row counts, constraint probes, sentinel retrieval
    (design/04 s.Backup contract). Safe to run against production's own
    connection -- the restore target is always a throwaway scratch DB this
    verb creates and drops, never the live one."""
    conninfo = _conninfo(database_url)
    migrations_path = pathlib.Path(migrations_dir) if migrations_dir else migrate.DEFAULT_MIGRATIONS_DIR
    try:
        log = verify_restore.run(
            pathlib.Path(against), conninfo, migrations_dir=migrations_path, sentinel_id=sentinel_id)
    except verify_restore.VerifyRestoreError as exc:
        raise typer.BadParameter(str(exc))
    for line in log:
        typer.echo(line)


@app.command("doctor")
def doctor_cmd(
    space: str = typer.Option(..., help="Space id to run hygiene checks against."),
    pack_file: str = typer.Option(
        None, "--pack-file",
        help="Pack file to diff the space's registries against for drift detection. "
             "Defaults to packs/<pack_name>/pack.yaml relative to cwd if not given."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Hygiene report (design/04 s.Hygiene): stale pendings, attrs_nonconforming
    counts, registry-vs-pack drift, orphaned merges, canonical chains >3, nodes
    with >20 addenda. Read-only -- never mutates the space it inspects."""
    conninfo = _conninfo(database_url)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        cur = conn.cursor()
        try:
            report = doctor.run(cur, space, pack_file=pathlib.Path(pack_file) if pack_file else None)
        except doctor.DoctorError as exc:
            raise typer.BadParameter(str(exc))
    for line in report:
        typer.echo(line)


@pack_app.command("upgrade")
def pack_upgrade(
    file: str = typer.Argument(..., help="Path to the new pack YAML/JSON file."),
    space: str = typer.Option(..., help="Space id to upgrade."),
    allow_nonconforming: bool = typer.Option(
        False, "--allow-nonconforming",
        help="For tightening changes with existing violators: apply anyway, leaving "
             "violating rows flagged (derivable, not blocked from writes that don't touch attrs, "
             "blocked from update() until fixed). Without this flag, a tightening change with "
             "violators is refused and a worklist is printed instead."),
    database_url: str = typer.Option(None, "--database-url", help="Overrides ENGRAPHY_DATABASE_URL."),
) -> None:
    """Diff the new pack's registry against the space's current one and apply
    by change class (design/04 s.Pack migrations): additive applied
    immediately; tightening applied only after a conformance scan (or with
    --allow-nonconforming); destructive refused while active rows/edges of
    that type exist."""
    conninfo = _conninfo(database_url)
    try:
        pack = packs.load_pack_file(file)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc))
    errors = packs.validate(pack)
    if errors:
        for err in errors:
            typer.echo(f"  - {err}")
        raise typer.BadParameter(f"pack '{file}' is invalid; not applied")
    format_warning = packs.check_pack_format(pack)
    if format_warning:
        typer.echo(f"warning: {format_warning}")
    same_topic_warning = packs.check_same_topic_declared(pack)
    if same_topic_warning:
        typer.echo(f"warning: {same_topic_warning}")

    with psycopg.connect(conninfo, autocommit=False) as conn:
        cur = conn.cursor()
        try:
            report = packs.upgrade(pack, space, cur, allow_nonconforming=allow_nonconforming)
        except packs.PackUpgradeRefused as exc:
            conn.rollback()
            for line in exc.worklist:
                typer.echo(f"  - {line}")
            raise typer.BadParameter(str(exc))
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback()
            raise typer.BadParameter(f"space '{space}' does not exist")
        except psycopg.Error as exc:
            conn.rollback()
            raise typer.BadParameter(str(exc).strip())
        conn.commit()
    for line in report:
        typer.echo(line)


if __name__ == "__main__":  # pragma: no cover
    app()
