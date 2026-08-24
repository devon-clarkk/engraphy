"""`engraphy-admin addenda promote` -- rescue facts already buried as get-only
addenda in existing stores (design/analysis/fact-searchability-model.md §2.6;
implementation fact-searchability-phase-b.md §3).

Before Phase B, a distinct-but-similar fact was absorbed into `attrs.addenda` on
its canonical, where it was never embedded and never in the search tsvector --
stored but unfindable. Phase B stops NEW absorption (merge-link). This command
recovers the OLD ones: for each active node with addenda, it re-runs the novelty
verdict per addendum (in merge order) against the cluster corpus and PROMOTES the
novel ones to their own embedded, searchable member nodes, `same_topic`-linked to
the canonical, exactly as a fresh merge-link would have stored them.

Local-CLI only (like `import`), never an MCP tool: it runs on a privileged
connection, scans across scopes, and sets `created_at` explicitly to the
addendum's own merge time so a promoted row's age is the merge's age, not the
migration's.

Idempotency is the `promoted_to` marker, committed in the SAME per-node
transaction as the member row: a re-run skips every marked addendum, so a run
after any crash promotes exactly the unmarked remainder and a full re-run is a
no-op. Embeddings are computed OUTSIDE the per-node transaction (trap 3).
"""
import re
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from engraphy.core import embedding
from engraphy.core.attr_spec import searchable_keys as _searchable_keys
from engraphy.core.dedup import ATTR_SURFACE_KEY, _config_bool
from engraphy.core.jaccard import is_novel

_AUDIT_ACTION = "addenda_promote"


def _surface_for_type(cur, space_id: str, node_type: str) -> tuple[set, bool]:
    """Sync sibling of dedup.resolve_attr_surface (this migration runs on a plain
    psycopg connection): the type's searchable keys + the space's write.attr_surface
    flag. Promoted members render their own surface (Phase C §2.4)."""
    cur.execute("SELECT attr_spec FROM node_types WHERE space_id = %s AND name = %s",
                (space_id, node_type))
    srow = cur.fetchone()
    cur.execute("SELECT value FROM config WHERE space_id = %s AND key = %s",
                (space_id, ATTR_SURFACE_KEY))
    crow = cur.fetchone()
    on = _config_bool(crow[0] if crow else None, ATTR_SURFACE_KEY, True)
    return _searchable_keys(srow[0] if srow and srow[0] is not None else {}), on

# First sentence terminator: ". ", "? ", "! ", or a newline. The space is
# required so "3.14" or "foo.bar" mid-token does not split a title.
_TERMINATOR = re.compile(r"[.?!] |\n")


@dataclass
class PromoteSummary:
    """What a promote run did. In --dry-run these are the WOULD-counts; nothing
    is written."""

    dry_run: bool = False
    nodes_scanned: int = 0
    addenda_seen: int = 0
    promoted: int = 0
    skipped_non_novel: int = 0
    skipped_already_marked: int = 0
    edges_skipped_missing_rule: int = 0

    def as_line(self) -> str:
        mode = "dry-run: would promote" if self.dry_run else "promoted"
        return (
            f"addenda promote ({'dry-run' if self.dry_run else 'applied'}): "
            f"{self.nodes_scanned} nodes scanned, {self.addenda_seen} addenda seen, "
            f"{mode} {self.promoted}, {self.skipped_non_novel} skipped (non-novel), "
            f"{self.skipped_already_marked} skipped (already promoted), "
            f"{self.edges_skipped_missing_rule} edges skipped (pack lacks same_topic rule)"
        )


def _derive_member_title(body: str, canonical_title: str) -> str:
    """Q4 (resolved by spec §3, veto-able): deterministic first-sentence
    derivation, no LLM (engine no-LLM rule). Take `body` up to the first sentence
    terminator, whitespace-strip; hard-truncate to 199 + ellipsis if it exceeds
    200 chars; if degenerate (< 3 chars) fall back to the canonical's title. The
    result always satisfies nodes' 3..200-char CHECK."""
    match = _TERMINATOR.search(body)
    first = (body[: match.start()] if match else body).strip()
    if len(first) > 200:
        first = first[:199] + "…"
    if len(first) < 3:
        return canonical_title[:188] + " — addendum"
    return first


def _member_title_and_attrs(addendum: dict, canonical_title: str) -> tuple[str, dict]:
    title = _derive_member_title(addendum.get("body", ""), canonical_title)
    attrs: dict = {}
    # Carry the reoccurrence date if the addendum has one (the error-reoccurrence
    # shape); otherwise the member starts attrs-clean.
    if addendum.get("happened_at") is not None:
        attrs["happened_at"] = addendum["happened_at"]
    return title, attrs


def _rule_present(cur, space_id, src_type, dst_type) -> bool:
    """§1.3 pre-check, sync: same exact-match lookup the merge path uses. Skip the
    edge (never abort the promote) when the pack declares no covering rule."""
    cur.execute(
        "SELECT 1 FROM edge_rules WHERE space_id = %s AND type = 'same_topic' "
        "AND src_type = %s AND dst_type = %s",
        (space_id, src_type, dst_type),
    )
    return cur.fetchone() is not None


def _peer_bodies(cur, space_id, canonical_id) -> list[str]:
    """Bodies of the canonical's existing same_topic peers, status-unfiltered
    (idempotency belt-and-braces; the promoted_to marker is the real guarantee)."""
    cur.execute(
        "SELECT n.body FROM edges e JOIN nodes n "
        "ON n.id = CASE WHEN e.src_id = %s THEN e.dst_id ELSE e.src_id END "
        "WHERE e.space_id = %s AND e.type = 'same_topic' "
        "AND (e.src_id = %s OR e.dst_id = %s)",
        (canonical_id, space_id, canonical_id, canonical_id),
    )
    return [body for (body,) in cur.fetchall()]


def _plan_node(cur, space_id, node) -> list[tuple[int, str, str, dict]]:
    """Read-phase, pure decision (no writes): which addenda of `node` to promote,
    in merge order. Returns [(addendum_index, title, body, member_attrs), ...].
    The running corpus starts at the canonical body + its same_topic peer bodies
    and grows with every addendum body seen -- promoted, marked, or skipped --
    so the verdict matches a fresh merge-link's cluster-corpus novelty check."""
    node_id, _n_type, _scope, title, body, attrs = node
    addenda = attrs.get("addenda", [])
    corpus_parts = [body, *_peer_bodies(cur, space_id, node_id)]
    plan = []
    for index, addendum in enumerate(addenda):
        a_body = addendum.get("body", "")
        if addendum.get("promoted_to") is not None:
            corpus_parts.append(a_body)  # already a member; keep it in the corpus
            continue
        if not is_novel(a_body, " ".join(corpus_parts)):
            corpus_parts.append(a_body)
            continue
        member_title, member_attrs = _member_title_and_attrs(addendum, title)
        plan.append((index, member_title, a_body, member_attrs))
        corpus_parts.append(a_body)
    return plan


def _vector_literal(vec) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


def promote_addenda(
    conn,
    space_id: str,
    scope_id: str | None = None,
    *,
    dry_run: bool = False,
    source_client: str = "engraphy-admin addenda promote",
    embed_document=embedding.embed_document,
) -> PromoteSummary:
    """Promote every novel, not-yet-promoted addendum in `space_id` (optionally a
    single `scope_id`) to its own searchable member node. Runs on `conn` (a
    privileged/superuser connection); each node's promotions commit in one
    transaction with their `promoted_to` markers. Embeddings are computed OUTSIDE
    those transactions.

    `embed_document` is injectable for tests (deterministic vectors); production
    passes the real model. Returns a PromoteSummary; in --dry-run nothing is
    written and the counts are the would-promote counts."""
    summary = PromoteSummary(dry_run=dry_run)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, type, scope_id, title, body, attrs FROM nodes "
        "WHERE space_id = %s AND status = 'active' "
        "AND (%s::text IS NULL OR scope_id = %s) "
        "AND jsonb_typeof(attrs -> 'addenda') = 'array' "
        "AND jsonb_array_length(attrs -> 'addenda') > 0 "
        "ORDER BY id",
        (space_id, scope_id, scope_id),
    )
    nodes = cur.fetchall()
    conn.commit()  # close the read transaction before embedding (trap 3)

    for node in nodes:
        node_id, node_type, node_scope, _node_title, _body, attrs = node
        summary.nodes_scanned += 1
        addenda = list(attrs.get("addenda", []))
        summary.addenda_seen += len(addenda)
        summary.skipped_already_marked += sum(
            1 for a in addenda if a.get("promoted_to") is not None
        )

        plan = _plan_node(cur, space_id, node)
        conn.commit()  # end the planning read before embedding

        # non-novel skips = unmarked addenda that were NOT planned for promotion.
        unmarked = sum(1 for a in addenda if a.get("promoted_to") is None)
        summary.skipped_non_novel += unmarked - len(plan)

        if not plan:
            continue

        # Embeddings OUTSIDE the per-node transaction (trap 3). Each member
        # renders its own searchable surface from its attrs (Phase C §2.4).
        keys, surface_on = _surface_for_type(cur, space_id, node_type)
        conn.commit()  # end the surface read before embedding
        embedded = []
        for index, title, body, member_attrs in plan:
            extra = embedding.render_attr_surface(member_attrs, keys) if surface_on else ""
            vec = embed_document(embedding.searchable_text(title, body, extra))
            embedded.append((index, title, body, member_attrs, extra, vec))

        if dry_run:
            summary.promoted += len(embedded)
            for index, title, _body, _attrs, _extra, _vec in embedded:
                # per-node detail (§3): what WOULD be promoted.
                rule_ok = _rule_present(cur, space_id, node_type, node_type)
                if not rule_ok:
                    summary.edges_skipped_missing_rule += 1
                print(f"  would promote node {node_id} addendum[{index}] "
                      f"-> member title {title!r}"
                      f"{'' if rule_ok else '  (edge skipped: no same_topic rule)'}")
            conn.commit()
            continue

        with conn.transaction():
            updated = list(addenda)
            for index, title, body, member_attrs, extra, vector in embedded:
                addendum = addenda[index]
                cur.execute(
                    "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, "
                    "embedding, embedding_model, source_client, source_session, "
                    "author_principal, created_at, extra_search) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (
                        space_id, node_type, node_scope, title, body, Jsonb(member_attrs),
                        _vector_literal(vector), embedding.MODEL_STAMP,
                        addendum.get("source_client"), addendum.get("source_session"),
                        addendum.get("author_principal"), addendum.get("merged_at"), extra,
                    ),
                )
                (member_id,) = cur.fetchone()

                if _rule_present(cur, space_id, node_type, node_type):
                    cur.execute(
                        "INSERT INTO edges (space_id, src_id, dst_id, type) "
                        "VALUES (%s, %s, %s, 'same_topic') "
                        "ON CONFLICT (src_id, dst_id, type) DO NOTHING",
                        (space_id, member_id, node_id),
                    )
                else:
                    summary.edges_skipped_missing_rule += 1

                cur.execute(
                    "INSERT INTO dedup_log (space_id, type, node_id, candidate_id, "
                    "similarity, band, author_principal) "
                    "VALUES (%s, %s, %s, %s, NULL, 'merge_linked_promoted', %s)",
                    (space_id, node_type, member_id, node_id,
                     addendum.get("author_principal") or ""),
                )
                cur.execute(
                    "INSERT INTO audit_log (space_id, principal, client_name, action, detail) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (space_id, addendum.get("author_principal") or "", source_client,
                     _AUDIT_ACTION,
                     Jsonb({"outcome": "merge_linked_promoted",
                            "node_id": str(member_id),
                            "canonical_id": str(node_id)})),
                )
                # mark the addendum in the array we will write back atomically.
                marked = dict(addendum)
                marked["promoted_to"] = str(member_id)
                updated[index] = marked
                summary.promoted += 1

            cur.execute(
                "UPDATE nodes SET attrs = jsonb_set(attrs, '{addenda}', %s::jsonb) WHERE id = %s",
                (Jsonb(updated), node_id),
            )

    return summary
