"""Dedup write path. Normative: design/implementation/dedup-write-path-plan.md.

Pipeline: validate + embed OUTSIDE txn; then one txn:
advisory lock (space,type) -> top-5 same-type candidates in writer-READABLE scope set
-> band (>=t_high merge | [t_low,t_high) pending | else insert) -> branch -> dedup_log
-> audit -> COMMIT -> resonance (read-only, excludes self).
Merge mechanics, pending world-change table, supersede self-exclusion: all in the plan.

Async: this is application orchestration inside engraphy/server/db.py's transaction()
wrapper (an async connection pool), not raw-SQL testing -- see that module's
docstring and DECISIONS-DELTA.md. embed() is the CALLER's job, done before write()
is invoked (trap 3: embed() must never run inside the transaction).
"""
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from engraphy.core import embedding, metrics
from engraphy.core.attr_spec import RESERVED_ATTR_KEYS
from engraphy.core.jaccard import is_novel
from engraphy.core.sentinel import RESERVED_NODE_TYPES
from engraphy.core.visibility import (
    ambient_scope_set_async,
    readable_scopes_async,
    writable_scopes_async,
)
from engraphy.server.db import transaction

_MAX_CANONICAL_CHAIN = 10

#: Static, always-present on `merged` envelopes (design/07 §Canonical tool I/O,
#: July 2026 revision; ruled 2026-07-21 from the dupstream contradiction
#: finding). Mirrors the pending envelope's `instruction` field.
#:
#: Why it exists: the first measured dupstream run auto-merged 28 of 36
#: contradiction pairs into the very fact they overturn. A negation is
#: lexically near-identical to its target ("Priya *no longer* works as a
#: paediatric nurse"), so it scores ABOVE t_high -- above the confirm band, on
#: the "same fact restated" premise negation breaks. Cosine similarity is
#: symmetric and structurally agreement-blind, and an adjudicating LLM in the
#: engine is forbidden (rule 4), so the engine cannot tell restatement from
#: negation and detection was rejected rather than faked.
#:
#: Judgment therefore stays the calling agent's (02's ratified non-goal) -- but
#: the shipped surface was misleading by omission: `addendum_added: true` reads
#: as "your information is stored", while the addendum is get-only and never
#: re-embedded, so search and briefing keep serving the STALE body as the
#: current fact. This string is the engine's half of that contract: it names
#: what to check and the verb that repairs it. Byte-pinned by 07's example and
#: fixtures/wire/write_merged.json -- do not reword without amending both.
MERGED_INSTRUCTION = (
    "Compare your write with the canonical node's body: if it contradicts or "
    "updates it rather than restating it, call supersede with old_id set to the "
    "canonical id to make your version the current fact."
)

# 07 §Exact formulas names this as config key `resonance.floor`, default 0.75.
# Carried as a default argument, matching BandThresholds' handling of the
# `dedup.t_high`/`dedup.t_low` config keys -- no code in this repo reads the
# config table yet (see the gate report).
RESONANCE_FLOOR = 0.75

# Typed exceptions carrying the design/07 §Error codes ENGRAPHY_<CODE> semantic.
# E1 raises these; E2's tool layer (not built yet) is what translates them
# into actual MCP wire errors. Only codes this module's own logic can actually
# reach are defined here, not a speculative full hierarchy. Attr-spec
# violations still need no class: the nodes_validate_attrs trigger (E0) raises
# psycopg.errors.CheckViolation with the exact byte-matching message, and
# likewise an illegal edge reaches the caller as edges_validate's CheckViolation
# (ENGRAPHY_EDGE_RULE at the tool layer) rather than anything raised here.


class NotFoundError(Exception):
    """ENGRAPHY_NOT_FOUND — unknown id, or a readable-permission failure
    (07: "deliberately identical for both -- existence is information")."""


class ScopeUnknownError(Exception):
    """ENGRAPHY_SCOPE_UNKNOWN — same not-found-shaped collapse, at scope level."""


class PendingExpiredError(Exception):
    """ENGRAPHY_PENDING_EXPIRED — resolve_duplicate after TTL, or the
    world-changed-so-re-judge cases (merge target archived/superseded)."""


class ValidationError(Exception):
    """ENGRAPHY_VALIDATION — a rule this layer checks itself, rather than one the
    E0 triggers raise as a CheckViolation. supersede()'s "same type as
    replacement -- cross-type supersession is a modeling error, rejected" and
    its status='active' precondition are the first such rules: neither is
    expressible as a table constraint (both compare the request against another
    row), so unlike attr validation there is no trigger to defer to."""


class SupersedeUnresolvedBandError(Exception):
    """NOT a 07 error code, and deliberately not dressed up as one -- this is a
    fail-closed guard over an UNSPECIFIED branch: QUESTIONS.md
    "supersede-nonclean-band" (open). dedup-write-path-plan.md §Supersede
    atomicity says to run the write pipeline with old_id excluded and then
    "insert supersedes edge; flip old" -- a sequence that presumes the pipeline
    inserted a node. Excluding old_id only stops the OLD node from banding; a
    *third* node can still push the replacement into MERGE (>= t_high) or
    PENDING ([t_low, t_high)), and no document says what supersede does then.
    PENDING is the sharp case: pending_writes' payload has no field carrying
    "this was a supersede", so parking would silently drop the supersession
    intent and leave old_id un-flipped. Raising rolls the transaction back,
    which keeps the plan's own guarantee ("kill-mid-call leaves the old node
    untouched") true for this branch too, until the question resolves."""


class ConfigError(Exception):
    """ENGRAPHY_INTERNAL-class — a per-space `config` row holds a value the write
    path can't use (non-numeric threshold, or a band ordering that violates
    0 < t_low <= t_high <= 1, or resonance.floor outside (0, 1]). config is NOT
    caller input (07's error table maps operator/config faults to INTERNAL), so
    a bad value fails the write LOUDLY rather than silently reverting to
    defaults -- silent fallback would be the same silently-ignored-config bug
    the config-read feature exists to fix, wearing a different hat (QUESTIONS.md
    per-space-config decision, 2026-07-16). E2's `engraphy-admin config set`
    validates at set time, so this should never fire in practice."""


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


async def _resolve_canonical(cur, node_id):
    """Chase canonical_id chains to the true canonical (trap #1: merging into
    an already-merged node). Chain cap 10, per the plan's trap list; a live
    write()'s own candidates are already status='active'-filtered so this
    matters most for resolve_duplicate's parked, possibly-stale candidate."""
    for _ in range(_MAX_CANONICAL_CHAIN):
        await cur.execute("SELECT status, canonical_id FROM nodes WHERE id = %s", (node_id,))
        status, canonical_id = await cur.fetchone()
        if status != "merged" or canonical_id is None:
            return node_id
        node_id = canonical_id
    raise RuntimeError("ENGRAPHY_INTERNAL: canonical chain exceeds 10 hops")


@dataclass
class BandThresholds:
    t_high: float = 0.95
    t_low: float = 0.80


def select_band(max_similarity: float, t: BandThresholds) -> str:
    """'merge' | 'pending' | 'insert' — boundary semantics: >= on both lower bounds."""
    if max_similarity >= t.t_high:
        return "merge"
    if max_similarity >= t.t_low:
        return "pending"
    return "insert"


_CONFIG_KEYS = ("dedup.t_high", "dedup.t_low", "resonance.floor")


def _config_float(rows: dict, key: str, default: float) -> float:
    """A per-space config value, or the code default when unset. jsonb loads as
    a Python object already, so a well-formed value arrives as int/float; a bool
    (jsonb true/false) is an int subclass and is rejected, as is a string."""
    if key not in rows:
        return default
    value = rows[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"ENGRAPHY_INTERNAL: config '{key}' must be a number, got {value!r}")
    return float(value)


ATTR_SURFACE_KEY = "write.attr_surface"


def _config_bool(value, key: str, default: bool) -> bool:
    """A per-space jsonb boolean config value, or the default when unset. Fails
    LOUD on a non-bool (ConfigError, ENGRAPHY_INTERNAL) for the same reason
    _config_float does -- config is operator input, and a silently-ignored bad
    value is the bug per-space config exists to fix."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"ENGRAPHY_INTERNAL: config '{key}' must be a boolean, got {value!r}")
    return value


async def resolve_attr_surface(pool, space_id: str, node_type: str) -> tuple[set, bool]:
    """Phase C (§2.1, §2.3): resolve, for the pre-embed step OUTSIDE any write
    transaction, (a) the node type's searchable attr keys and (b) whether the
    space's `write.attr_surface` flag is on (default true). node_types and config
    are non-RLS reference tables (conftest's _READ_ONLY_TABLES), so a BARE pool
    connection reads them -- deliberately NOT the transaction() RLS wrapper, which
    would open an identity-GUC transaction purely to read reference data
    (DECISIONS-DELTA). The caller renders `extra` with the returned keys iff the
    flag is on and passes it + the embedded vector into write()."""
    from engraphy.core.attr_spec import searchable_keys as _searchable_keys

    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT attr_spec FROM node_types WHERE space_id = %s AND name = %s",
            (space_id, node_type),
        )
        row = await cur.fetchone()
        attr_spec = row[0] if row and row[0] is not None else {}
        await cur.execute(
            "SELECT value FROM config WHERE space_id = %s AND key = %s",
            (space_id, ATTR_SURFACE_KEY),
        )
        crow = await cur.fetchone()
    on = _config_bool(crow[0] if crow else None, ATTR_SURFACE_KEY, True)
    return _searchable_keys(attr_spec), on


async def embed_with_attr_surface(pool, space_id, node_type, title, body, attrs):
    """Phase C (§2.4): the ONE pre-write step every writer runs before write()/
    supersede()/import/promote -- resolve the type's searchable keys + the space's
    flag, render the extra surface (or '' when the flag is off), and embed
    searchable_text(title, body, extra). Returns (embedding_vector, extra_search):
    the caller hands both to the write path, which stores extra_search and uses
    the vector. Embedding stays OUTSIDE any transaction (trap 3). Centralized here
    so the render/embed pairing is defined once and CI-grepped (only this module's
    embed_document call sites pass searchable_text output)."""
    keys, on = await resolve_attr_surface(pool, space_id, node_type)
    extra = embedding.render_attr_surface(attrs or {}, keys) if on else ""
    vector = embedding.embed_document(embedding.searchable_text(title, body, extra))
    return vector, extra


async def _resolve_config(cur, space_id, thresholds, resonance_floor):
    """Per-space governance values, resolved once per write inside the write
    transaction (QUESTIONS.md per-space-config, 2026-07-16, Fable): one indexed
    lookup of all three keys, NO caching -- a config change is effective on the
    next write. Precedence: explicit caller parameter > config row > code
    default (BandThresholds' 0.95/0.80, RESONANCE_FLOOR 0.75).

    Space-safety is the in-query `space_id` filter, exactly as edge_rules,
    node_types and every other space-scoped reference table is read (config is
    non-RLS reference data, in conftest's _READ_ONLY_TABLES, not _RLS_TABLES).
    Fable's decision text assumed config had an RLS policy; it does not, and
    adding one would make config the lone RLS-covered reference table --
    DECISIONS-DELTA.md. `space_id` here is the token's own, set by the wrapper,
    the same value the advisory lock and candidate query already trust.

    Only config-sourced values are range-validated (fail loud via ConfigError);
    a caller-passed BandThresholds/floor is trusted test surface."""
    await cur.execute(
        "SELECT key, value FROM config WHERE space_id = %s AND key = ANY(%s)",
        (space_id, list(_CONFIG_KEYS)),
    )
    rows = {key: value for key, value in await cur.fetchall()}

    if thresholds is None:
        defaults = BandThresholds()
        bands = BandThresholds(
            t_high=_config_float(rows, "dedup.t_high", defaults.t_high),
            t_low=_config_float(rows, "dedup.t_low", defaults.t_low),
        )
        if not (0.0 < bands.t_low <= bands.t_high <= 1.0):
            raise ConfigError(
                f"ENGRAPHY_INTERNAL: dedup thresholds must satisfy 0 < t_low <= t_high <= 1, "
                f"got t_low={bands.t_low}, t_high={bands.t_high}"
            )
    else:
        bands = thresholds

    if resonance_floor is None:
        floor = _config_float(rows, "resonance.floor", RESONANCE_FLOOR)
        if not (0.0 < floor <= 1.0):
            raise ConfigError(
                f"ENGRAPHY_INTERNAL: resonance.floor must be in (0, 1], got {floor}"
            )
    else:
        floor = resonance_floor

    return bands, floor


def _validate_links_shape(links) -> None:
    """write.links item = {type, src_id?, dst_id?} with EXACTLY ONE endpoint
    present -- the omitted end is the node being written (QUESTIONS.md
    links-wire-shape, 2026-07-16, Fable option c). Pure and structural: run
    before the transaction opens, so a malformed request never takes the
    advisory lock. Malformed -> ValidationError (07 ENGRAPHY_VALIDATION).

    Registry/rule faults are NOT checked here -- BOTH an unregistered edge type
    AND a registered type with no matching (type, src, dst) rule surface as the
    edges_validate trigger's CheckViolation ("edge_rules: no rule for type=..."),
    because that BEFORE-INSERT trigger queries edge_rules and fires before the
    edges->edge_types FK is ever reached; an unregistered type simply has no
    edge_rules row either. The E2 tool layer maps that CheckViolation to
    ENGRAPHY_EDGE_RULE (QUESTIONS.md links-wire-shape review, 2026-07-16, Fable:
    "unknown edge type as EDGE_RULE-class from the registry", correcting the
    original decision's VALIDATION clause -- an unregistered type is a missing
    rule-matrix row, matching the supersede-edge-type precedent). The error-class
    split is therefore: pure shape here (VALIDATION), endpoint existence at
    attach time (NOT_FOUND), type/rule at the DB (EDGE_RULE)."""
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get("type"), str):
            raise ValidationError("ENGRAPHY_VALIDATION: each link requires a string 'type'")
        extra = set(link) - {"type", "src_id", "dst_id"}
        if extra:
            raise ValidationError(
                f"ENGRAPHY_VALIDATION: link has unknown field(s) {sorted(extra)}; "
                f"allowed: type, src_id, dst_id"
            )
        has_src = link.get("src_id") is not None
        has_dst = link.get("dst_id") is not None
        if has_src == has_dst:
            raise ValidationError(
                "ENGRAPHY_VALIDATION: each link must name exactly one of src_id/dst_id "
                "(the omitted endpoint is the node being written)"
            )


def _validate_no_reserved_attrs(attrs: dict) -> None:
    """`attrs.addenda` is reserved for the merge-history append target
    (Q1) -- a caller-supplied value would be silently clobbered by the first
    merge, or worse, spoof merge history that never happened. Checked before
    the transaction opens, same as `_validate_links_shape`, so a malformed
    request never takes the advisory lock."""
    if not attrs:
        return
    for key in sorted(RESERVED_ATTR_KEYS & set(attrs)):
        raise ValidationError(f"ENGRAPHY_VALIDATION: attrs.{key} is a reserved key")


def _validate_not_reserved_type(node_type: str) -> None:
    """Engine-owned node types (RESERVED_NODE_TYPES -- today just
    `engram_sentinel`) may not be written over the tool surface (ruled
    2026-07-21; design/07 §Pack file schema, Reserved names).

    `space create` registers the sentinel type in `node_types` so the sentinel
    row can exist, and that registry row is exactly what `nodes.(space_id,
    type)` FK-checks -- so without this, any caller who guessed the name got a
    valid insert. Not a leak (the rows are archived-filtered out of search,
    briefing and dedup candidacy, and a new row cannot alter the minted one),
    but `doctor`, `pack upgrade`, `verify-restore` and both read-path type
    exclusions all special-case this type on the premise that its rows are
    engine-minted, and under no-hard-deletes the junk would be permanent.

    Deliberately application-layer and NOT a DDL CHECK: a constraint cannot
    distinguish the engine's own mint on the privileged `space create` path
    from an agent's write without teaching the kernel about call provenance.
    The mint runs on the CLI path, the refusal on the pipeline path.

    Checked before the transaction opens, same as `_validate_links_shape` and
    `_validate_no_reserved_attrs`, so a refused request never takes the
    advisory lock."""
    if node_type in RESERVED_NODE_TYPES:
        raise ValidationError(
            f"ENGRAPHY_VALIDATION: type '{node_type}' is reserved for the engine "
            f"and cannot be written"
        )


async def _attach_links(cur, space_id, node_id, links) -> tuple[int, int]:
    """Attach request links to node_id and return (attached, skipped). Each item
    names exactly one explicit endpoint (shape pre-validated); the omitted end is
    node_id, so `src_id` present => edge (peer -> node_id), `dst_id` present =>
    edge (node_id -> peer) -- directionally unambiguous, which is the whole point
    of the explicit-endpoint shape.

    The peer's readability is checked FIRST via an RLS-filtered SELECT: an
    unknown OR unreadable id collapses to ENGRAPHY_NOT_FOUND (06: existence is
    information), and gating here means the edges FK never gets to raise its own
    "endpoint does not exist" in the wrong error class. The insert is
    ON CONFLICT DO NOTHING so a link already present is skipped, not an error
    (rowcount => attached vs skipped); a (type, src, dst) with no edge_rules row
    still raises the trigger's CheckViolation (re-rule-checked at resolution for
    parked links -- trap 8)."""
    if not links:
        return 0, 0
    attached = skipped = 0
    for link in links:
        if link.get("src_id") is not None:
            peer, src_id, dst_id = link["src_id"], link["src_id"], node_id
        else:
            peer, src_id, dst_id = link["dst_id"], node_id, link["dst_id"]
        await cur.execute("SELECT 1 FROM nodes WHERE id = %s AND space_id = %s", (peer, space_id))
        if await cur.fetchone() is None:
            raise NotFoundError(f"ENGRAPHY_NOT_FOUND: link endpoint {peer} not found")
        await cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (src_id, dst_id, type) DO NOTHING",
            (space_id, src_id, dst_id, link["type"]),
        )
        if cur.rowcount == 1:
            attached += 1
        else:
            skipped += 1
    return attached, skipped


async def write(
    pool,
    space_id: str,
    principal: str,
    node_type: str,
    scope_id: str,
    title: str,
    body: str,
    attrs: dict,
    embedding_vector: list[float],
    source_client: str,
    thresholds: BandThresholds | None = None,
    import_mode: bool = False,
    resonance_floor: float | None = None,
    links: list[dict] | None = None,
    source_session: str | None = None,
    action: str = "write",
    extra_search: str = "",
    _between_branch_and_commit=None,
) -> dict:
    """The whole pipeline: steps 3-8 in one transaction, then step 9's
    resonance report after COMMIT (INSERT + MERGE + PENDING branches).

    extra_search: Phase C (§2.4) -- the rendered searchable-attr text
    (render_attr_surface output), computed by the caller alongside the embedding
    (both OUTSIDE the transaction). Stored verbatim in nodes.extra_search on any
    node this write creates (insert or merge-link member). Defaults "" so a caller
    that does not compute it -- every test passing a synthetic vector, every
    pre-Phase-C code path -- writes a node byte-identical to Phase B. The vector
    the caller passes must have been embedded from searchable_text(title, body,
    extra_search); write() trusts that pairing exactly as it trusts the vector.

    action: the audit_log `action` value (default "write"). A pack alias
    dispatches here with the caller's args merged, but the audit row must
    still record the alias identity ("write via log_error", design/03 §Tool
    aliases) so provenance survives the sugar -- app.py passes
    `aliases.resolve_alias_call`'s third return value through as this param;
    a direct write() call never sets it and gets plain "write".

    source_session: QUESTIONS.md "write-session-id-argument" (5.6) -- the wire
    `session_id` argument, stored as `nodes.source_session`. A thin
    passthrough (the column has existed since E0); its purpose is
    `purge-session`'s threat window (design/04): a parked or merged write's
    provenance must survive park/merge or a poisoned session can never be
    fully reached later.

    _between_branch_and_commit: the plan's crash-test seam ("a test hook ...
    between steps 6 and COMMIT"). An async callback(cur) awaited after the
    branch + dedup_log + audit_log writes but before the transaction commits;
    tests inject a raise or a backend kill here to prove the whole write is one
    atomic unit. Production callers never pass it.

    links: plan step 6's "insert links, rule-checked", now with a wire shape
    (QUESTIONS.md links-wire-shape, 2026-07-16, Fable option c): each item
    {type, src_id?|dst_id?} names one explicit endpoint, the other being this
    write's node. Attached on INSERT and (with ON CONFLICT + counts) on MERGE;
    parked verbatim on PENDING and applied at resolution (trap 8). Shape is
    validated before the transaction, so a malformed request never takes the
    advisory lock. Opens its own transaction and delegates to `_locked_core`,
    the same helper `resolve_duplicate`'s `distinct` path re-enters -- one
    lock+candidate+band+branch implementation, not two.

    import_mode: the bulk-import caller (engraphy/admin/import_.py, a later E1
    module -- this flag has no consumer yet) sets this. See _locked_core.

    thresholds / resonance_floor: None means "read the per-space config, else
    fall to code defaults" (_resolve_config); an explicit value wins over both.
    """
    _validate_links_shape(links or [])
    _validate_no_reserved_attrs(attrs)
    # Covers import and inbox_review(promote) too -- both funnel through here,
    # so all four write-pipeline entry points inherit the refusal with no
    # tool-layer change. An import row naming the type fails the RUN loudly
    # (the ValidationError propagates out of run_import, which has no per-row
    # catch), exactly as a reserved `attrs.addenda` key does today.
    _validate_not_reserved_type(node_type)
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        # QUESTIONS.md "write-scope-writable-precheck" (resolved 2026-07-18):
        # the pre-check lives here, not the tool layer, so every caller (the
        # write tool, inbox_review(promote), import) inherits it from one
        # place -- the same writable_scopes_async pattern resolve_duplicate
        # already uses (dedup-write-path-plan.md's world-change table:
        # "Scope became unwritable for the author -> ENGRAPHY_SCOPE_UNKNOWN,
        # not-found semantics"). RLS WITH CHECK remains the backstop.
        writable = await writable_scopes_async(cur)
        if scope_id not in writable:
            raise ScopeUnknownError(
                f"ENGRAPHY_SCOPE_UNKNOWN: scope '{scope_id}' does not exist or is not writable"
            )
        bands, floor = await _resolve_config(cur, space_id, thresholds, resonance_floor)
        envelope = await _locked_core(
            cur, space_id, principal, node_type, scope_id, title, body,
            attrs, embedding_vector, source_client, bands, import_mode=import_mode,
            links=links, source_session=source_session, action=action,
            extra_search=extra_search,
            _between_branch_and_commit=_between_branch_and_commit,
        )
    # Q2: import_mode skips the resonance report entirely -- the returned
    # envelope for an import-mode branch has no `resonance` key at all (not
    # an empty list). run_import already discards whatever write() returns,
    # so this changes nothing durable, only the report-shaped tail.
    if import_mode:
        return envelope
    # Usage metric (design/03 §stats): a fresh write is exactly one of
    # facts_stored (inserted) or duplicates_prevented (merged / merged_linked /
    # needs_confirmation). Best-effort, in its OWN transaction after the write
    # above committed -- never fails or rolls back the write it counts. Covers
    # inbox promote too (it funnels through write()); import_mode is already
    # excluded by the guard above (its review_queued outcome is off the MCP
    # surface). supersede does not reach here (it calls _locked_core directly).
    metric = metrics.write_outcome_metric(envelope["outcome"])
    if metric:
        await metrics.bump_safe(pool, space_id, principal, {metric: 1})
    # Step 9, deliberately outside the block above: the plan runs resonance
    # after COMMIT, as its own read-only statement.
    return await _attach_resonance(
        pool, space_id, principal, embedding_vector, envelope, floor
    )


async def _locked_core(
    cur, space_id, principal, node_type, scope_id, title, body, attrs,
    embedding_vector, source_client, thresholds, collapse_pending_to_insert=False,
    exclude_id=None, import_mode=False, links=None, source_session=None,
    action="write", extra_search="", _between_branch_and_commit=None,
) -> dict:
    """Steps 3-8: advisory lock -> candidate query -> band -> branch ->
    dedup_log -> audit_log. Runs inside a caller-supplied, already-open
    transaction (write()'s own, or resolve_duplicate's).

    import_mode: trap 6's "same pipeline ... No third code path -- the flags
    gate steps 6-PENDING and the envelope only". Both gates live in the band
    branch below and change nothing else: same lock, same candidate query, same
    band arithmetic, same dedup_log/audit_log rows. An import that hits INSERT
    is byte-identical to a normal write.

    exclude_id: supersede()'s trap #2 defence -- the one node the candidate
    query must not see (dedup-write-path-plan.md §Supersede atomicity: "run the
    write pipeline with `old_id` excluded from the candidate set (the
    replacement is *supposed* to be ~0.9-similar to what it replaces)").
    Without it every supersede would band PENDING against the very node it
    replaces and park instead of writing. write()/resolve_duplicate never set it.

    collapse_pending_to_insert: resolve_duplicate's `distinct` path re-enters
    this function to satisfy the world-change table's "Insert re-runs the
    full band check (another writer may have inserted the twin meanwhile)."
    But the human/agent has *already* resolved the 0.80-0.95 ambiguity by
    choosing `distinct` -- re-parking at the SAME still-pending similarity
    would silently ignore that choice. Only a *new* >= t_high hit (a twin
    that landed while this write sat parked) should change the outcome, per
    the plan's own "A >= 0.95 hit at resolve-time converts the distinct into
    a merge-with-notice" -- so PENDING collapses to INSERT here; only MERGE
    stays MERGE. write() never sets this (a fresh write's PENDING is real).
    """
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s || ':' || %s, 0))",
        (space_id, node_type),
    )

    candidate_scopes = await ambient_scope_set_async(cur, {scope_id})
    await cur.execute(
        "SELECT id, embedding <=> %s::vector AS distance FROM nodes "
        "WHERE space_id = %s AND type = %s AND status = 'active' "
        "AND scope_id = ANY(%s) AND (%s::uuid IS NULL OR id <> %s::uuid) "
        "ORDER BY distance LIMIT 5",
        (
            _vector_literal(embedding_vector), space_id, node_type, list(candidate_scopes),
            exclude_id, exclude_id,
        ),
    )
    candidates = await cur.fetchall()

    best_candidate_id = None
    max_similarity = 0.0
    if candidates:
        best_candidate_id, best_distance = min(candidates, key=lambda row: row[1])
        max_similarity = 1.0 - best_distance

    band = select_band(max_similarity, thresholds)
    if band == "pending" and collapse_pending_to_insert:
        band = "insert"
    candidate_similarity = max_similarity if candidates else None
    # dedup_log's band value: the merge branch refines it to 'merge_linked' when
    # the novel-content path fires (Phase B §2.1); every other branch logs `band`.
    log_band = band

    if band == "merge":
        # Split the >= t_high band on the cluster novelty verdict (§2.1):
        # non-novel absorbs into the canonical (node_id_for_log = canonical),
        # novel merge-links a new member row (node_id_for_log = member). The
        # dedup_log candidate_id stays best_candidate_id (pre-canonical
        # resolution, as before), even under import_mode -- "silent" drops the
        # REPORT, not the mechanics.
        canonical_id = await _resolve_canonical(cur, best_candidate_id)
        node_id_for_log, log_band, envelope = await _merge_branch(
            cur, space_id, canonical_id, principal, source_client, max_similarity,
            node_type, scope_id, title, body, attrs, embedding_vector, links, source_session,
            extra_search,
        )
        if import_mode:
            # "AUTO-MERGE absorbs silently" (02 §Bulk import): the mechanics
            # (addendum or member row + edge, dedup_log/audit_log below) already
            # ran; only the surfaced report is dropped. For the absorb outcome,
            # `addendum_added` survives the strip -- internal plumbing for the CLI
            # importer (run_import tallies the False cases into
            # ImportSummary.merged_addendum_dropped, the only tell a backfill row
            # was absorbed with its text discarded). A merge-link kept the text
            # (member row), so it needs no such tell and collapses to just the
            # outcome (§2.2 import-mode).
            if envelope["outcome"] == "merged":
                envelope = {"v": 1, "outcome": "merged",
                            "addendum_added": envelope["addendum_added"]}
            else:  # merged_linked
                envelope = {"v": 1, "outcome": "merged_linked"}
    elif band == "pending":
        node_id_for_log = None
        if import_mode:
            # Trap 6: "PENDING -> review_queue CSV row instead of parking". No
            # pending_writes row exists to resolve, because an import is not an
            # interactive session -- the row goes to a report a human/agent
            # triages afterwards (02 §Bulk import). Links are dropped with it:
            # there is nothing to park them on, and the reviewer re-issues the
            # write with its links if the item survives triage.
            envelope = await _do_review_queue(cur, title, body, candidates)
        else:
            envelope = await _do_pending(
                cur, space_id, principal, node_type, scope_id, title, body, attrs,
                embedding_vector, source_client, candidates, links, source_session,
                extra_search,
            )
    else:
        node_id_for_log, envelope = await _do_insert(
            cur, space_id, node_type, scope_id, principal, source_client,
            title, body, attrs, embedding_vector, source_session, extra_search,
        )
        # INSERT band: attach links to the brand-new node (rule-checked). No
        # counts in the `inserted` envelope (07 gives it none); a fresh node has
        # no pre-existing edges, but ON CONFLICT still no-ops a duplicate item.
        await _attach_links(cur, space_id, node_id_for_log, links)

    await cur.execute(
        "INSERT INTO dedup_log (space_id, type, node_id, candidate_id, similarity, band, "
        "author_principal) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (space_id, node_type, node_id_for_log, best_candidate_id, candidate_similarity, log_band, principal),
    )
    await cur.execute(
        "INSERT INTO audit_log (space_id, principal, client_name, action, detail) "
        "VALUES (%s, %s, %s, %s, %s)",
        (space_id, principal, source_client, action,
         Jsonb({
             "outcome": envelope["outcome"],
             "node_id": str(node_id_for_log) if node_id_for_log is not None else None,
         })),
    )

    # Crash-test seam: everything above this line is written; the caller's
    # transaction has not yet committed. A raise or a backend kill here must
    # leave zero rows in all five write-path tables (plan §Test plan crash row).
    if _between_branch_and_commit is not None:
        await _between_branch_and_commit(cur)

    return envelope


async def _do_insert(cur, space_id, node_type, scope_id, principal, source_client, title, body, attrs, embedding_vector, source_session=None, extra_search=""):
    await cur.execute(
        "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
        "embedding_model, source_client, source_session, author_principal, extra_search) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s) "
        "RETURNING id, status, created_at",
        (
            space_id, node_type, scope_id, title, body, Jsonb(attrs),
            _vector_literal(embedding_vector), embedding.MODEL_ID, source_client,
            source_session, principal, extra_search,
        ),
    )
    node_id, status, created_at = await cur.fetchone()
    # Q1: strip defensively, for uniformity with the merge path -- a fresh
    # insert has no addenda yet, but this keeps the invariant regression-proof.
    if "addenda" in attrs:
        attrs = {k: v for k, v in attrs.items() if k != "addenda"}
    envelope = {
        "v": 1,
        "outcome": "inserted",
        "node": {
            "id": str(node_id),
            "type": node_type,
            "scope": scope_id,
            "title": title,
            "body": body,
            "attrs": attrs,
            "status": status,
            "author": principal,
            "created_at": created_at.isoformat(),
        },
    }
    return node_id, envelope


async def _candidate_rows(cur, candidates) -> list[dict]:
    """The 07 `candidates` entry shape, nearest-first (the candidate query's own
    ORDER BY distance). Shared by the PENDING envelope and the import-mode
    review-queue row so the two can never describe the same candidate
    differently."""
    rows = []
    for candidate_id, distance in candidates:
        await cur.execute("SELECT title, body FROM nodes WHERE id = %s", (candidate_id,))
        c_title, c_body = await cur.fetchone()
        rows.append({
            "id": str(candidate_id),
            "title": c_title,
            "body": c_body,
            "similarity": round(1.0 - distance, 2),
        })
    return rows


async def _do_review_queue(cur, title, body, candidates) -> dict:
    """Import mode's PENDING branch: no pending_writes row, no interactive
    resolution -- just the data 02 §Bulk import names for the report ("CSV:
    incoming, candidate, similarity"), carried back for engraphy/admin/import_.py
    to write. Exactly those three things and nothing else: this envelope is an
    internal admin-CLI shape, not a 07 wire contract (import "is admin-CLI only,
    not an MCP tool"), and it has no consumer yet -- so inventing extra fields
    the real importer may not want would be guessing (DECISIONS-DELTA.md)."""
    candidate_rows = await _candidate_rows(cur, candidates)
    nearest = candidate_rows[0]  # a PENDING band always has >= 1 candidate
    return {
        "v": 1,
        "outcome": "review_queued",
        "incoming": {"title": title, "body": body},
        "candidate": nearest,
        "similarity": nearest["similarity"],
    }


async def _do_pending(cur, space_id, principal, node_type, scope_id, title, body, attrs, embedding_vector, source_client, candidates, links=None, source_session=None, extra_search="") -> dict:
    """No node row. Parks the payload + embedding + candidates snapshot for
    resolve_duplicate (24h TTL). `payload`'s internal jsonb shape isn't
    specified anywhere (pending_writes' own migration comment: "a single
    jsonb column so E1 doesn't need a DDL change") -- DECISIONS-DELTA-level
    boring gap, not a wire contract (only the *response* envelope below is
    07-normative).

    Links ride along verbatim (trap 8: "parked with the payload, applied on
    resolution, re-rule-checked then"). Their shape was already validated at
    write() time; the rule/endpoint re-check happens when resolve_duplicate
    attaches them, against a world that may have moved."""
    candidate_rows = await _candidate_rows(cur, candidates)

    payload = {
        "type": node_type,
        "scope_id": scope_id,
        "title": title,
        "body": body,
        "attrs": attrs,
        "source_client": source_client,
        "source_session": source_session,
        "embedding": embedding_vector,
        "candidates": candidate_rows,
        "links": links or [],
        # Phase C: park the rendered searchable surface with the payload, so the
        # node inserted at resolution stores the same extra_search the parked
        # embedding was computed from (the vector and extra_search must stay
        # paired across the park). Absent on pre-C parked rows -> '' at resolution.
        "extra_search": extra_search,
    }
    await cur.execute(
        "INSERT INTO pending_writes (space_id, author_principal, payload, expires_at) "
        "VALUES (%s, %s, %s, now() + interval '24 hours') RETURNING id, expires_at",
        (space_id, principal, Jsonb(payload)),
    )
    pending_id, expires_at = await cur.fetchone()

    return {
        "v": 1,
        "outcome": "needs_confirmation",
        "pending_id": str(pending_id),
        "expires_at": expires_at.isoformat(),
        "candidates": candidate_rows,
        "instruction": "Call resolve_duplicate with resolution 'distinct' or 'merge'.",
    }


async def _cluster_rule_present(cur, space_id, src_type, dst_type) -> bool:
    """Phase B §1.3 graceful-skip pre-check: is there a `same_topic` edge_rules
    row covering (src_type -> dst_type) in this space? Rules are wildcard-EXPANDED
    at pack-apply time (packs.expand_edge_rules), so this is an exact-match lookup
    -- the same plain-lookup shape the edges_validate trigger itself uses. We
    check the rule ourselves and skip the INSERT when it is absent, rather than
    letting the trigger raise a CheckViolation inside the write transaction: a
    CheckViolation would poison the whole transaction and abort a LEGAL write
    (I1: a fact must never fail to store because a pack predates the edge type)."""
    await cur.execute(
        "SELECT 1 FROM edge_rules WHERE space_id = %s AND type = 'same_topic' "
        "AND src_type = %s AND dst_type = %s",
        (space_id, src_type, dst_type),
    )
    return await cur.fetchone() is not None


async def _same_topic_peer_bodies(cur, space_id, canonical_id) -> list[str]:
    """Bodies of the canonical's existing `same_topic` peers (the edge reaches
    either endpoint). Status-UNFILTERED on purpose (§2.1 step 1): a superseded
    member's content still counts as "already represented", so a fact restated
    after being merge-linked once is judged non-novel against the cluster and
    absorbs rather than spawning a duplicate member."""
    await cur.execute(
        "SELECT n.body FROM edges e JOIN nodes n "
        "ON n.id = CASE WHEN e.src_id = %s THEN e.dst_id ELSE e.src_id END "
        "WHERE e.space_id = %s AND e.type = 'same_topic' "
        "AND (e.src_id = %s OR e.dst_id = %s)",
        (canonical_id, space_id, canonical_id, canonical_id),
    )
    return [body for (body,) in await cur.fetchall()]


async def _merge_branch(
    cur, space_id, canonical_id, principal, source_client, similarity,
    node_type, scope_id, title, body, attrs, embedding_vector, links, source_session,
    extra_search="",
) -> tuple[str, str, dict]:
    """The >= t_high merge outcome, split on the novelty verdict the code already
    computes (Phase B §2.1). Shared by _locked_core's merge branch and
    resolve_duplicate's `merge` path so the split has one implementation.

    canonical_id is already chain-resolved (trap #1). Returns
    (node_id_for_log, log_band, envelope):
    - NON-novel (J >= 0.8 against the cluster corpus): absorb, exactly as before
      -- provenance merge, addendum only on the error-reoccurrence special case.
      -> (canonical_id, "merge", merged-envelope).
    - novel (J < 0.8): merge-link -- the incoming inserts as its own embedded,
      searchable member row with its OWN attrs, a `same_topic` edge links it to
      the canonical, and the envelope reports `merged_linked`.
      -> (member_id, "merge_linked", merged_linked-envelope).

    The corpus (canonical body + addenda bodies + same_topic peer bodies) and the
    novelty verdict are computed ONCE here and handed to _do_absorb, so neither
    is recomputed (§2.1 step 3)."""
    await cur.execute(
        "SELECT type, scope_id, title, body, attrs, status, author_principal, created_at "
        "FROM nodes WHERE id = %s",
        (canonical_id,),
    )
    c_type, c_scope, c_title, c_body, c_attrs, c_status, c_author, c_created_at = await cur.fetchone()

    addenda = list(c_attrs.get("addenda", []))
    peer_bodies = await _same_topic_peer_bodies(cur, space_id, canonical_id)
    corpus = c_body + " " + " ".join(a.get("body", "") for a in addenda) + " " + " ".join(peer_bodies)
    novel = is_novel(body, corpus)

    if novel:
        envelope = await _do_merge_link(
            cur, space_id, canonical_id, c_type, c_scope, c_title,
            node_type, scope_id, principal, source_client, title, body, attrs,
            embedding_vector, similarity, links, source_session, extra_search,
        )
        return envelope["node"]["id"], "merge_linked", envelope

    envelope = await _do_absorb(
        cur, space_id, canonical_id, c_type, c_scope, c_title, c_body, c_attrs,
        c_status, c_author, c_created_at, addenda, principal, source_client,
        similarity, body, attrs, links, source_session,
    )
    return canonical_id, "merge", envelope


async def _do_merge_link(
    cur, space_id, canonical_id, c_type, c_scope, c_title,
    node_type, scope_id, principal, source_client, title, body, attrs,
    embedding_vector, similarity, links, source_session, extra_search="",
) -> dict:
    """Phase B §2.1 novel branch: keep BOTH. INSERT the incoming as its own node
    (via the very same _do_insert every insert uses -- the member keeps its own
    title/body/attrs/embedding/provenance AND its own extra_search surface, which
    the old absorb path discarded), attach the request's links to the MEMBER (it
    exists now; the omitted-endpoint convention maps to the member, matching the
    INSERT branch), then attach a `same_topic` edge member -> canonical subject to
    the §1.3 pre-check."""
    member_id, insert_env = await _do_insert(
        cur, space_id, node_type, scope_id, principal, source_client,
        title, body, attrs, embedding_vector, source_session, extra_search,
    )
    # Links attach to the member, not the canonical (unlike the absorb path).
    await _attach_links(cur, space_id, member_id, links)

    # Same-type by construction (dedup candidates are same-type), so node_type
    # is also the canonical's type; the pre-check uses (node_type, c_type).
    cluster_edge_added = await _cluster_rule_present(cur, space_id, node_type, c_type)
    if cluster_edge_added:
        # member -> canonical (matches the distinct path's new-node -> candidate
        # convention). ON CONFLICT keeps it idempotent under retries.
        await cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) "
            "VALUES (%s, %s, %s, 'same_topic') "
            "ON CONFLICT (src_id, dst_id, type) DO NOTHING",
            (space_id, member_id, canonical_id),
        )

    return {
        "v": 1,
        "outcome": "merged_linked",
        "node": insert_env["node"],
        "canonical": {
            "id": str(canonical_id),
            "type": c_type,
            "scope": c_scope,
            "title": c_title,
        },
        "similarity": round(similarity, 2),
        "cluster_edge_added": cluster_edge_added,
    }


async def _do_absorb(
    cur, space_id, canonical_id, c_type, c_scope, c_title, c_body, c_attrs,
    c_status, c_author, c_created_at, addenda, principal, source_client,
    similarity, incoming_body, incoming_attrs, links=None, source_session=None,
) -> dict:
    """Phase B §2.1 non-novel branch: today's provenance merge, unchanged in
    behavior. Called only when the incoming is NOT Jaccard-novel against the
    cluster corpus, so the addendum is added only on the error-reoccurrence
    special case (a reoccurrence with non-novel wording is a provenance event,
    not a distinct fact -- a novel reoccurrence merge-links instead). Takes the
    canonical row + addenda pre-read by _merge_branch to avoid re-reading."""
    # error-type special case (plan step 6-MERGE item 2): a re-occurrence is
    # data even when the wording is identical -- one addendum record.
    is_reoccurrence = (
        c_type == "error"
        and incoming_attrs.get("happened_at") is not None
        and incoming_attrs.get("happened_at") != c_attrs.get("happened_at")
    )

    addendum_added = False
    if is_reoccurrence:
        await cur.execute("SELECT now()")
        (merged_at,) = await cur.fetchone()
        addendum = {
            "merged_at": merged_at.isoformat(),
            "source_client": source_client,
            "source_session": source_session,
            "author_principal": principal,
            "body": incoming_body,
            # QUESTIONS.md "error-reoccurrence-addendum-shape" (open): keep the
            # occurrence date, or "a re-occurrence is data" loses the datum.
            "happened_at": incoming_attrs.get("happened_at"),
        }
        addenda.append(addendum)
        await cur.execute(
            "UPDATE nodes SET attrs = jsonb_set(attrs, '{addenda}', %s::jsonb) WHERE id = %s "
            "RETURNING attrs",
            (Jsonb(addenda), canonical_id),
        )
        (c_attrs,) = await cur.fetchone()
        addendum_added = True

    # plan step 6-MERGE item 3: attach the request's links to the canonical node
    # (the omitted endpoint maps to the canonical, not the incoming).
    links_attached, links_skipped = await _attach_links(cur, space_id, canonical_id, links)

    # Q1: strip addenda from the wire attrs -- never ships in `attrs`.
    wire_attrs = {k: v for k, v in c_attrs.items() if k != "addenda"}

    return {
        "v": 1,
        "outcome": "merged",
        "canonical": {
            "id": str(canonical_id),
            "type": c_type,
            "scope": c_scope,
            "title": c_title,
            "body": c_body,
            "attrs": wire_attrs,
            "status": c_status,
            "author": c_author,
            "created_at": c_created_at.isoformat(),
        },
        "similarity": round(similarity, 2),
        "addendum_added": addendum_added,
        "links_attached": links_attached,
        "links_skipped": links_skipped,
        "instruction": MERGED_INSTRUCTION,
    }


async def resolve_duplicate(
    pool,
    space_id: str,
    principal: str,
    pending_id: str,
    resolution: str,
    merge_into: str | None = None,
    thresholds: BandThresholds | None = None,
    resonance_floor: float | None = None,
) -> dict:
    """dedup-write-path-plan.md §Pending resolution against a changed world.
    Re-validates inside its own locked transaction; a parked write never
    bypasses current schema or a world that moved on since parking.

    Carries a resonance report for the same reason write() does: 07 says this
    "returns the `write` envelope of the final outcome", and both of its
    outcomes (inserted / merged) are non-PENDING writes -- the exact set 07's
    resonance rule covers."""
    if resolution not in ("distinct", "merge"):
        raise ValueError(f"unknown resolution: {resolution!r}")
    if resolution == "merge" and not merge_into:
        raise ValueError("resolution='merge' requires merge_into")

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        thresholds, floor = await _resolve_config(cur, space_id, thresholds, resonance_floor)

        await cur.execute(
            "SELECT payload, expires_at FROM pending_writes WHERE id = %s AND space_id = %s",
            (pending_id, space_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"ENGRAPHY_NOT_FOUND: pending write {pending_id} not found")
        payload, expires_at = row

        # TTL: checked here, never relies on the nightly sweep (plan: "resolution
        # checks expires_at itself and never relies on the sweep").
        await cur.execute("SELECT now()")
        (now,) = await cur.fetchone()
        if now >= expires_at:
            raise PendingExpiredError(
                f"ENGRAPHY_PENDING_EXPIRED: pending write {pending_id} expired at {expires_at.isoformat()}"
            )

        writable = await writable_scopes_async(cur)
        if payload["scope_id"] not in writable:
            raise ScopeUnknownError(f"ENGRAPHY_SCOPE_UNKNOWN: scope {payload['scope_id']} is not writable")

        if resolution == "distinct":
            # Parked links re-enter the SAME _locked_core path (trap 8: applied
            # on resolution, re-rule-checked then -- the edge rules may have
            # changed while the write sat parked). A distinct that collapses to
            # INSERT attaches them to the new node; one that finds a fresh twin
            # at >= t_high MERGEs and attaches them to the canonical instead.
            envelope = await _locked_core(
                cur, space_id, principal, payload["type"], payload["scope_id"], payload["title"],
                payload["body"], payload["attrs"], payload["embedding"], payload["source_client"],
                thresholds, collapse_pending_to_insert=True, links=payload.get("links"),
                source_session=payload.get("source_session"),
                extra_search=payload.get("extra_search", ""),
            )
            if envelope["outcome"] == "inserted":
                envelope["relates_edge_added"] = await _attach_relates_to_nearest_active(
                    cur, space_id, envelope["node"]["id"], payload["candidates"],
                )
        else:
            # "Candidate got merged -- chase canonical_id; proceed against the
            # canonical." Also covers "candidate archived/superseded" once
            # chased to its final resting node.
            canonical_id = await _resolve_canonical(cur, merge_into)
            await cur.execute("SELECT status FROM nodes WHERE id = %s", (canonical_id,))
            target = await cur.fetchone()
            if target is None:
                raise NotFoundError(f"ENGRAPHY_NOT_FOUND: merge target {merge_into} not found")
            if target[0] in ("archived", "superseded"):
                raise PendingExpiredError(
                    f"ENGRAPHY_PENDING_EXPIRED: merge target {merge_into} is no longer active; write again"
                )
            similarity = next(
                (c["similarity"] for c in payload["candidates"] if c["id"] == str(merge_into)), 1.0
            )
            # An explicit merge goes through the SAME novelty split as an
            # auto-merge (§2.2): novel content returns `merged_linked` (the
            # caller's "these belong together" intent is honored AS A LINK, and
            # I1 outranks the absorb reading), non-novel absorbs. The dedup_log
            # band stays in sync with the outcome (merge / merge_linked).
            node_id_for_log, log_band, envelope = await _merge_branch(
                cur, space_id, canonical_id, principal, payload["source_client"],
                similarity, payload["type"], payload["scope_id"], payload["title"],
                payload["body"], payload["attrs"], payload["embedding"],
                payload.get("links"), payload.get("source_session"),
                payload.get("extra_search", ""),
            )
            await cur.execute(
                "INSERT INTO dedup_log (space_id, type, node_id, candidate_id, similarity, band, "
                "author_principal) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (space_id, payload["type"], node_id_for_log, merge_into, similarity, log_band, principal),
            )

        await cur.execute("DELETE FROM pending_writes WHERE id = %s", (pending_id,))

    # Usage metric (design/03 §stats): a resolution that INSERTED a new node
    # (the `distinct` path that did not find a fresh twin) counts as
    # facts_stored. A `merge`/`merged_linked` resolution is NOT recounted here --
    # the originating write already counted this interaction as
    # duplicates_prevented at needs_confirmation time (see core.metrics). Best-
    # effort, own transaction after the resolution above committed.
    if envelope["outcome"] == "inserted":
        await metrics.bump_safe(pool, space_id, principal, {metrics.FACTS_STORED: 1})

    return await _attach_resonance(
        pool, space_id, principal, payload["embedding"], envelope, floor
    )


async def _attach_relates_to_nearest_active(cur, space_id, new_node_id, candidate_snapshot) -> bool:
    """distinct path: "auto-adds a relates_to-class edge to the nearest
    candidate" (DECISIONS-DELTA: verified 'relates_to', literal, exists
    wildcard-scoped in both shipped packs). Nearest STILL-ACTIVE candidate
    from the original snapshot, or none -- graceful, not an error, if every
    snapshotted candidate has since gone inactive."""
    for candidate in candidate_snapshot:  # already nearest-first from _do_pending
        await cur.execute("SELECT status FROM nodes WHERE id = %s", (candidate["id"],))
        row = await cur.fetchone()
        if row is not None and row[0] == "active":
            await cur.execute(
                "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'relates_to') "
                "ON CONFLICT (src_id, dst_id, type) DO NOTHING",
                (space_id, new_node_id, candidate["id"]),
            )
            return True
    return False


async def supersede(
    pool,
    space_id: str,
    principal: str,
    old_id: str,
    node_type: str,
    scope_id: str,
    title: str,
    body: str,
    attrs: dict,
    embedding_vector: list[float],
    source_client: str,
    thresholds: BandThresholds | None = None,
    resonance_floor: float | None = None,
    source_session: str | None = None,
    links: list[dict] | None = None,
    extra_search: str = "",
) -> dict:
    """dedup-write-path-plan.md §Supersede atomicity, in its stated order, all
    in ONE transaction: validate the old node (readable, writable scope, status
    'active', same type as the replacement) -> run the write pipeline with
    old_id excluded from the candidate set (trap #2) -> insert the `supersedes`
    edge -> flip old to status='superseded'.

    Atomicity is the transaction's, not this function's: any raise below (or a
    kill mid-call) rolls the whole thing back, so the old node is never left
    flipped without its replacement, nor a replacement written without the flip.

    links: same wire shape as write()'s (packs.py's CORE_TOOLS declares
    'links' a valid supersede argument); attached to the freshly-inserted
    replacement node via _locked_core's own INSERT-branch link attach, rule-
    checked exactly as a plain write's links are. Shape is validated before
    the transaction opens, same as write().

    Returns the inserted node's `write` envelope plus `"superseded": <old_id>`.
    07 gives no canonical I/O shape for supersede (the only tool it omits);
    mirroring resolve_duplicate's stated rule -- "returns the `write` envelope
    of the final outcome" -- is the boring reading (DECISIONS-DELTA.md).
    """
    _validate_links_shape(links or [])
    _validate_no_reserved_attrs(attrs)
    # A second call site, not a redundant one: supersede does NOT go through
    # write() -- its replacement's type is a distinct caller-supplied argument
    # validated before its own transaction opens. With cross-type supersession
    # already rejected below, refusing a sentinel-typed replacement here makes
    # the sentinel unsupersedable through the tool surface.
    _validate_not_reserved_type(node_type)
    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()
        thresholds, floor = await _resolve_config(cur, space_id, thresholds, resonance_floor)

        # RLS filters unreadable rows out entirely, so "no row" covers both a
        # bad id and a readable-permission failure -- 07's deliberate collapse
        # ("existence is information"), same as resolve_duplicate's lookup.
        await cur.execute(
            "SELECT type, scope_id, status FROM nodes WHERE id = %s AND space_id = %s",
            (old_id, space_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"ENGRAPHY_NOT_FOUND: node {old_id} not found")
        old_type, old_scope, old_status = row

        writable = await writable_scopes_async(cur)
        if old_scope not in writable:
            raise ScopeUnknownError(
                f"ENGRAPHY_SCOPE_UNKNOWN: scope {old_scope} is not writable"
            )
        # Mirrors write()'s writable-scope precheck (QUESTIONS.md
        # "write-scope-writable-precheck", resolved 2026-07-18) for the
        # REPLACEMENT's target scope -- old_scope's own writability is
        # checked above, but scope_id (where the new node lands) is a
        # separate, caller-supplied argument that can differ from old_scope,
        # and without this check an unwritable scope_id would reach
        # _locked_core's INSERT and surface as a raw RLS error instead of a
        # clean ENGRAPHY_SCOPE_UNKNOWN.
        if scope_id not in writable:
            raise ScopeUnknownError(
                f"ENGRAPHY_SCOPE_UNKNOWN: scope '{scope_id}' does not exist or is not writable"
            )
        if old_status != "active":
            raise ValidationError(
                f"ENGRAPHY_VALIDATION: old_id must name an active node; node {old_id} "
                f"has status '{old_status}'"
            )
        if old_type != node_type:
            raise ValidationError(
                f"ENGRAPHY_VALIDATION: type must equal the superseded node's type "
                f"('{old_type}'), not '{node_type}' -- cross-type supersession is a "
                f"modeling error"
            )

        envelope = await _locked_core(
            cur, space_id, principal, node_type, scope_id, title, body, attrs,
            embedding_vector, source_client, thresholds, exclude_id=old_id,
            source_session=source_session, links=links, extra_search=extra_search,
        )
        if envelope["outcome"] not in ("inserted", "merged_linked"):
            # Fail closed over an unspecified branch (QUESTIONS.md
            # "supersede-nonclean-band", narrowed by the Q2 ruling). `inserted`
            # and `merged_linked` BOTH produce a replacement row as
            # envelope["node"], so both let the supersede complete: if the
            # replacement bands >= t_high against a *third* node and its content
            # is novel, it merge-links to that node (an accepted consequence --
            # a supersede may create a cluster with an unrelated-but-similar
            # third node; §2.3 item 5). Only outcomes with NO replacement row
            # remain fail-closed: `needs_confirmation` (the PENDING sub-case, the
            # sharp one -- pending_writes carries no "this was a supersede" flag),
            # and the rare `merged` (a replacement non-novel against a third
            # node's cluster, absorbed with no row of its own).
            raise SupersedeUnresolvedBandError(
                f"supersede's replacement banded '{envelope['outcome']}' against a node "
                f"other than old_id and produced no replacement row; the plan does not "
                f"define this case (QUESTIONS.md 'supersede-nonclean-band', PENDING/absorb "
                f"sub-cases)"
            )

        new_id = envelope["node"]["id"]
        # src = the replacement, dst = the old node ("Replacement; the old
        # node's status becomes superseded" -- both packs' edge_types wording).
        # No ON CONFLICT: new_id was just inserted, so no edge can pre-exist.
        # A pack with no matching supersedes rule fails here on E0's
        # edges_validate trigger, which E2's tool layer renders as
        # ENGRAPHY_EDGE_RULE (QUESTIONS.md "supersede-edge-type-pack-
        # inconsistency", resolved: that error path is the intended behavior
        # wherever a pack declines a supersession).
        await cur.execute(
            "INSERT INTO edges (space_id, src_id, dst_id, type) VALUES (%s, %s, %s, 'supersedes')",
            (space_id, new_id, old_id),
        )
        await cur.execute("UPDATE nodes SET status = 'superseded' WHERE id = %s", (old_id,))
        envelope["superseded"] = str(old_id)

    return await _attach_resonance(
        pool, space_id, principal, embedding_vector, envelope, floor
    )


async def _resonance_report(cur, space_id, embedding_vector, exclude_id, floor) -> list[dict]:
    """07 §Exact formulas, Resonance report: "top-3 nodes by similarity >= 0.75
    (config `resonance.floor`), any type, writer-readable scopes,
    status='active', excluding the node just written/merged-into".

    Read-only, and the plan runs it AFTER COMMIT as a separate statement -- so
    the just-written row is committed and would be its own best hit at
    similarity 1.0 if exclude_id didn't drop it (trap 5, "resonance self-hit").

    Note the two deliberate widenings vs the dedup candidate query above: ANY
    type (not same-type), and the READABLE scope set (not the writable/ambient
    candidate set) -- "the dedup query already ran; widening it to any-type is
    one more index scan" (02 §Resonance reports). Readability is the bound that
    makes a report unable to leak a teammate's private memory (06), and it is
    enforced twice: explicitly here, and by nodes' RLS policy on this very
    SELECT.
    """
    readable = await readable_scopes_async(cur)
    if not readable:
        return []
    q = _vector_literal(embedding_vector)
    # similarity >= floor  <=>  cosine distance <= 1 - floor. Filtering on
    # distance keeps the pgvector index usable, where filtering on a computed
    # similarity would not.
    await cur.execute(
        "SELECT id, type, scope_id, title, embedding <=> %s::vector AS distance FROM nodes "
        "WHERE space_id = %s AND status = 'active' AND id <> %s::uuid "
        "AND scope_id = ANY(%s) AND (embedding <=> %s::vector) <= %s "
        "ORDER BY distance, id LIMIT 3",
        (q, space_id, str(exclude_id), list(readable), q, 1.0 - floor),
    )
    hits = await cur.fetchall()

    report = []
    for node_id, node_type, scope_id, title, distance in hits:
        report.append({
            "id": str(node_id),
            "type": node_type,
            "scope": scope_id,
            "title": title,
            "similarity": round(1.0 - distance, 2),
            "links": await _one_hop_links(cur, space_id, node_id),
        })
    return report


async def _one_hop_links(cur, space_id, node_id) -> list[dict]:
    """A resonant node's one-hop link summary: `{type, direction, peer_id,
    peer_title}`, <= 5, readable-filtered (07). `direction` is relative to the
    resonant node -- 'out' = it is the edge's src.

    Readable-filtered falls out of composition rather than being re-derived: the
    join to `nodes` is RLS-filtered, so an edge to an unreadable peer drops out
    here, and `edges`' own policy already required both endpoints readable
    (visibility-and-rls-plan.md). ORDER BY is for determinism only -- 07 caps
    the list at 5 but names no order, and an unordered LIMIT would make the
    envelope nondeterministic (DECISIONS-DELTA.md).
    """
    await cur.execute(
        "SELECT type, direction, peer_id, peer_title FROM ("
        "  SELECT e.type, 'out' AS direction, n.id AS peer_id, n.title AS peer_title"
        "  FROM edges e JOIN nodes n ON n.id = e.dst_id"
        "  WHERE e.space_id = %s AND e.src_id = %s"
        "  UNION ALL"
        "  SELECT e.type, 'in', n.id, n.title"
        "  FROM edges e JOIN nodes n ON n.id = e.src_id"
        "  WHERE e.space_id = %s AND e.dst_id = %s"
        ") links ORDER BY direction, type, peer_id LIMIT 5",
        (space_id, node_id, space_id, node_id),
    )
    return [
        {"type": t, "direction": d, "peer_id": str(pid), "peer_title": ptitle}
        for t, d, pid, ptitle in await cur.fetchall()
    ]


def _resonance_exclude_id(envelope) -> str | None:
    """"the node just written/merged-into" -- or None for an envelope that wrote
    no node, which is exactly the set of outcomes that get no report: PENDING
    ("After a NON-PENDING write"), import mode's review_queued (same band, no
    node), and import mode's silent merge (trap 6 gates the envelope, and a
    resonance report is the most report-shaped part of it)."""
    outcome = envelope.get("outcome")
    if outcome == "inserted":
        return envelope["node"]["id"]
    if outcome == "merged_linked":
        # Phase B: a merge-link IS a newly written node (the member), so by the
        # rule's own logic ("the node just written") it excludes itself and gets
        # a resonance report, exactly as an insert does. The import-mode collapse
        # drops the `node` key, but import mode skips resonance entirely upstream,
        # so this only ever sees the full envelope.
        return envelope["node"]["id"]
    if outcome == "merged" and "canonical" in envelope:
        return envelope["canonical"]["id"]
    return None


async def _attach_resonance(pool, space_id, principal, embedding_vector, envelope, floor) -> dict:
    """Pipeline step 9: runs AFTER the write transaction has committed, in its
    own read-only transaction -- which is also the only way it can be
    RLS-filtered, since db.py's transaction() wrapper is the single place the
    identity GUCs are ever set (visibility-and-rls-plan.md §Session GUC
    protocol)."""
    exclude_id = _resonance_exclude_id(envelope)
    if exclude_id is None:
        return envelope
    async with transaction(pool, space_id, principal) as conn:
        envelope["resonance"] = await _resonance_report(
            conn.cursor(), space_id, embedding_vector, exclude_id, floor
        )
    return envelope
