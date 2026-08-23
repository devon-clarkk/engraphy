"""Declarative briefing engine. Normative: design/02 §The briefing engine;
section grammar schema: design/07 §Pack file schema ($defs.briefing).
Sections execute in pack order inside ONE transaction (the E1 briefing gate's
reading of 02's "sections run concurrently" perf note: concurrency is a
budget lever for later, not an output contract -- order in the envelope is
pack order either way; see DECISIONS-DELTA.md). Empty sections included with
nodes: []. Fixtures: engraphy/tests/fixtures/briefing/.

The grammar interpreter is deliberately dumb: each section compiles to one
parameterized SELECT over `nodes` (the engine doesn't know what a "decision"
is -- 02). The `semantic: true` section is the exception: it runs the SAME
hybrid implementation search uses (search.hybrid_fuse) over the caller's
`hint`, and returns nodes: [] when the hint is absent or blank -- there is
no query to run (E1 briefing gate, Q2).

Scope set: requested scope ∪ reader's readable ambient scopes; 'all' = every
readable scope, and (03's exfiltration tripwire, same as search) writes one
audit_log row. include_linked pulls FULL linked nodes, readability-filtered
by the edges/nodes RLS composition, uncapped (gate Q3: a briefing's linked
checks are the payload, not decoration; unreadable targets drop silently, no
placeholder). Non-semantic sections cap at 10 (gate Q1), ordered by
order_by_attr asc when given, else updated_at desc; id asc tie-breaks both
into a total order. Footer inbox_pending is the 14-day NAG: it counts pending
rows OLDER than 14 days (created_at <= now() - 14d) in the scope set PLUS
unscoped (scope_id IS NULL) rows -- an unpinned capture surfaces in every
briefing rather than being orphaned (gate; RLS policy: migration 0013).
Recall stats bump for every surfaced node, linked included (they were shown).
"""
import datetime
import re

from psycopg.types.json import Jsonb

from engraphy.core.dedup import ConfigError, _config_float
from engraphy.core.embedding import embed_query
from engraphy.core.recall import bump_recall
from engraphy.core.sentinel import SENTINEL_NODE_TYPE
from engraphy.core.scope_set import resolve_scope_set
from engraphy.core.search import _node_envelope, hybrid_fuse
from engraphy.server.db import transaction

_SECTION_CAP = 10       # non-semantic section cap (gate Q1)
_DEFAULT_TOP_K = 10     # semantic top_k when the pack omits it (schema maximum)
_DEFAULT_SEMANTIC_FLOOR = 0.50   # briefing.semantic_floor default (relevance floor)


async def _resolve_semantic_floor(cur, space_id, caller_floor):
    """The vector-leg relevance floor for semantic sections (02 §The briefing
    engine; QUESTIONS.md "semantic-section-relevance-floor", Devon option b).
    Precedence caller-param > config `briefing.semantic_floor` > 0.50 default,
    read per-call inside the briefing transaction (no cache), same governance
    family as resonance.floor. A config-sourced value out of (0, 1] or non-numeric
    fails loud via ConfigError; a caller override is trusted test surface."""
    if caller_floor is not None:
        return caller_floor
    await cur.execute(
        "SELECT value FROM config WHERE space_id = %s AND key = 'briefing.semantic_floor'",
        (space_id,),
    )
    row = await cur.fetchone()
    rows = {"briefing.semantic_floor": row[0]} if row else {}
    floor = _config_float(rows, "briefing.semantic_floor", _DEFAULT_SEMANTIC_FLOOR)
    if not (0.0 < floor <= 1.0):
        raise ConfigError(
            f"ENGRAPHY_INTERNAL: briefing.semantic_floor must be in (0, 1], got {floor}"
        )
    return floor

_RELATIVE_DATE_RE = re.compile(r"^([+-])(\d+)d$")

# The 9 envelope columns _node_envelope expects, in its order.
_ENVELOPE_COLS = "id, type, scope_id, title, body, attrs, status, author_principal, created_at"


def _resolve_date_bound(value: str) -> str:
    """where_attr before/after bound -> ISO date string. Accepts ISO dates or
    the +Nd relative form (design/07 cross-reference validation's grammar);
    relative is anchored to the server's current date. Comparison downstream
    is TEXT comparison -- ISO dates sort lexicographically identically to
    chronologically, and a cast would turn one malformed attr value in an
    open-typed node into a whole-section error (DECISIONS-DELTA.md)."""
    m = _RELATIVE_DATE_RE.match(value)
    if not m:
        return value  # ISO date, pack-validate checked
    sign, days = m.groups()
    delta = datetime.timedelta(days=int(days))
    # Local calendar date, not UTC (ruff DTZ011): REL:+2d resolves against
    # user-authored attribute dates like a due date, which are calendar dates
    # rather than instants. "Today" here must mean the operator's today.
    today = datetime.date.today()  # noqa: DTZ011
    return (today + delta if sign == "+" else today - delta).isoformat()


def _compile_section(section: dict, space_id: str, scope_set: set[str]) -> tuple[str, list]:
    """One non-semantic section -> one parameterized SELECT over nodes.
    Filters: type/types, status (default 'active'), where_attr (equals /
    before / after, text comparison on ISO dates), recent (created_at window),
    without_edge (RLS-composed NOT EXISTS -- an edge whose far endpoint is
    unreadable does not exist for this reader, consistent with 06).
    Order: order_by_attr asc NULLS LAST when given, else updated_at desc;
    id asc tie-break makes either a total order. Cap 10."""
    sql = f"SELECT {_ENVELOPE_COLS} FROM nodes WHERE space_id = %s AND scope_id = ANY(%s)"
    params: list = [space_id, list(scope_set)]

    types = [section["type"]] if "type" in section else list(section.get("types", []))
    if types:
        sql += " AND type = ANY(%s)"
        params.append(types)

    # Excluded by type, not by status: a pack may declare a section with
    # `status: archived`, which would otherwise pull the engine's restore
    # sentinel into a briefing (design/04 requires it never surface there).
    sql += " AND type <> %s"
    params.append(SENTINEL_NODE_TYPE)

    sql += " AND status = %s"
    params.append(section.get("status", "active"))

    where_attr = section.get("where_attr") or {}
    if where_attr:
        key = where_attr["key"]
        if "equals" in where_attr:
            sql += " AND attrs->>%s = %s"
            params += [key, where_attr["equals"]]
        if "before" in where_attr:
            sql += " AND attrs->>%s < %s"
            params += [key, _resolve_date_bound(where_attr["before"])]
        if "after" in where_attr:
            sql += " AND attrs->>%s > %s"
            params += [key, _resolve_date_bound(where_attr["after"])]

    if "recent" in section:
        days = int(section["recent"][:-1])  # schema pattern ^[0-9]+d$
        sql += " AND created_at >= now() - make_interval(days => %s)"
        params.append(days)

    without_edge = section.get("without_edge") or {}
    if without_edge:
        near = "dst_id" if without_edge.get("direction", "out") == "in" else "src_id"
        sql += (
            f" AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.space_id = %s"
            f" AND e.type = %s AND e.{near} = nodes.id)"
        )
        params += [space_id, without_edge["edge"]]

    if "order_by_attr" in section:
        sql += " ORDER BY attrs->>%s ASC NULLS LAST, updated_at DESC, id ASC"
        params.append(section["order_by_attr"])
    else:
        sql += " ORDER BY updated_at DESC, id ASC"
    sql += " LIMIT %s"
    params.append(_SECTION_CAP)
    return sql, params


async def _linked_nodes(cur, space_id, node_ids, edge_type, direction):
    """include_linked: FULL linked-node envelopes per section node, uncapped,
    readability-filtered by RLS composition (edges policy already requires
    both endpoints readable; the join to nodes re-applies nodes_read).
    direction 'out': section node is src, linked = dst; 'in' mirrors.
    Ordered (updated_at desc, id asc) within each node -- the same total
    order sections use (boring gap, DECISIONS-DELTA.md)."""
    if not node_ids:
        return {}
    near, far = ("src_id", "dst_id") if direction == "out" else ("dst_id", "src_id")
    cols = ", ".join("n." + c.strip() for c in _ENVELOPE_COLS.split(","))
    await cur.execute(
        f"SELECT e.{near}, {cols} "
        f"FROM edges e JOIN nodes n ON n.id = e.{far} "
        f"WHERE e.space_id = %s AND e.type = %s AND e.{near} = ANY(%s::uuid[]) "
        f"ORDER BY n.updated_at DESC, n.id ASC",
        (space_id, edge_type, node_ids),
    )
    linked = {}
    for row in await cur.fetchall():
        linked.setdefault(str(row[0]), []).append(row[1:])
    return linked


async def briefing(
    pool,
    space_id: str,
    principal: str,
    scope: str,
    hint: str | None,
    source_client: str,
    config: dict,
    semantic_floor: float | None = None,
    no_scope_all: bool = False,
) -> dict:
    """Execute a pack's `briefing:` config (design/02 §The briefing engine) and
    return the 07 §Canonical I/O briefing envelope: {"v": 1, "sections":
    [{"name", "nodes": [{…node…, "linked?": […]}]}], "footer":
    {"inbox_pending": N}} -- section order = pack order; empty sections
    included with nodes: []; nodes are always FULL detail (07: briefing has
    no detail parameter).

    scope: a specific scope id or 'all' (audited). hint: free text driving
    `semantic: true` sections; absent/blank -> those sections are empty.
    config: the applied pack's briefing fragment ({"sections": […],
    "footer": {…}}) -- the caller (server tool layer / tests) owns pack
    lookup; the engine stays pack-agnostic.
    no_scope_all: the CALLING TOKEN's scope restriction (api_tokens.no_scope_all,
    migration 0023), threaded from AuthContext by the tool dispatcher. True means
    scope='all' is refused outright (ENGRAPHY_SCOPE) instead of resolved -- see
    core.scope_set. Defaults False so every direct/test caller keeps the old
    unrestricted behaviour.
    """
    sections_cfg = config.get("sections", [])
    footer_cfg = config.get("footer") or {}
    hint_text = (hint or "").strip()

    has_semantic = any(s.get("semantic") for s in sections_cfg)
    # One embedding for every semantic section, OUTSIDE the transaction
    # (trap 3) -- and none at all when no semantic section will run.
    hint_vec = None
    if hint_text and has_semantic:
        hint_vec = embed_query(hint_text)

    async with transaction(pool, space_id, principal) as conn:
        cur = conn.cursor()

        # Relevance floor resolved per-call inside the txn, only when a semantic
        # section exists (a floorless briefing never reads the config key).
        floor = await _resolve_semantic_floor(cur, space_id, semantic_floor) if has_semantic else None

        scope_set = await resolve_scope_set(cur, scope, no_scope_all, "briefing")

        sections_out = []
        surfaced = []
        for section in sections_cfg:
            nodes_out = []
            if scope_set:
                if section.get("semantic"):
                    rows = await _semantic_rows(
                        cur, space_id, section, scope_set, hint_vec, hint_text, floor
                    )
                else:
                    sql, params = _compile_section(section, space_id, scope_set)
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()

                include_linked = section.get("include_linked") or {}
                linked_map = None
                if include_linked:
                    linked_map = await _linked_nodes(
                        cur, space_id, [str(r[0]) for r in rows],
                        include_linked["edge"], include_linked.get("direction", "out"),
                    )
                for row in rows:
                    nid = str(row[0])
                    node = _node_envelope(row, "full")
                    surfaced.append(nid)
                    if linked_map is not None:
                        node["linked"] = [
                            _node_envelope(lr, "full") for lr in linked_map.get(nid, [])
                        ]
                        surfaced += [ln["id"] for ln in node["linked"]]
                    nodes_out.append(node)
            sections_out.append({"name": section["name"], "nodes": nodes_out})

        footer = {}
        if footer_cfg.get("inbox_pending_count"):
            # The 14-day NAG (02 §The inbox): count only pending rows OLDER than
            # 14 days (created_at <= now() - 14d), not every pending row -- the
            # footer nags about items left un-triaged, not fresh captures (Devon,
            # 2026-07-17, option b). Scope set PLUS unscoped rows (NULL scope_id
            # surfaces in every briefing -- gate decision; RLS 0013 already denies
            # rows in unreadable scopes, the explicit predicate narrows to the
            # REQUESTED set within that).
            await cur.execute(
                "SELECT count(*) FROM inbox WHERE space_id = %s AND status = 'pending' "
                "AND created_at <= now() - interval '14 days' "
                "AND (scope_id = ANY(%s) OR scope_id IS NULL)",
                (space_id, list(scope_set)),
            )
            footer["inbox_pending"] = (await cur.fetchone())[0]

        # Recall stats for every node the briefing SHOWED, linked included
        # (02 "recall stats batched on every read"; 0012 keeps updated_at
        # stable). Deduplicated: one briefing = one recall per node.
        await bump_recall(cur, list(dict.fromkeys(surfaced)))

        if scope == "all":
            await cur.execute(
                "INSERT INTO audit_log (space_id, principal, client_name, action, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (space_id, principal, source_client, "briefing",
                 Jsonb({"scope": "all", "sections": len(sections_out),
                        "nodes": len(set(surfaced))})),
            )

    return {"v": 1, "sections": sections_out, "footer": footer}


async def _semantic_rows(cur, space_id, section, scope_set, hint_vec, hint_text, vector_floor):
    """A `semantic: true` section: hybrid search over the hint via the SAME
    implementation search uses (search.hybrid_fuse), filtered to the section's
    types, capped at top_k, with the relevance floor applied to the vector leg.
    No hint (or blank) -> no query to run -> [] (Q2)."""
    if not hint_text or hint_vec is None:
        return []
    types = [section["type"]] if "type" in section else list(section.get("types", [])) or None
    top_k = section.get("top_k", _DEFAULT_TOP_K)
    top, _similarity, node_map, _truncated = await hybrid_fuse(
        cur, space_id, hint_vec, hint_text, scope_set, types,
        False,  # semantic sections surface active memory only
        top_k,
        vector_floor=vector_floor,
    )
    return [node_map[nid] for nid, _ in top]
