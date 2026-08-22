"""Read-path recall stats. Normative: design/02 (§Search "Recall stats batched
on every read"; §Non-goals "recall_count/last_recalled_at are collected from day
one"). QUESTIONS.md "recall-stats-vs-nodes-touch" resolved 2026-07-16 (Devon,
option a): the nodes_touch trigger (migration 0012) leaves updated_at untouched
for a recall-only update, so a read can bump recall stats without pretending the
node was edited.

One helper, called by every read path (search now; get / traverse / briefing as
they land) so the semantics live in one place: +1 recall_count, last_recalled_at
= now(), for exactly the nodes the read SURFACED, in ONE batched statement.
"""


async def bump_recall(cur, node_ids) -> None:
    """Batched recall bump for the nodes a read returned. Runs inside the read's
    own transaction (a plain UPDATE; the trigger keeps updated_at stable). The
    ids are the SURFACED results -- nodes the caller actually saw -- not every
    scanned candidate. Empty -> no-op.

    E1 is single-principal, so every returned node is owner-writable and the
    nodes UPDATE RLS policy (USING readable / WITH CHECK writable) is satisfied.
    The readable-but-not-writable case (a teammate's team-read scope) only exists
    once E6 introduces read-without-write grants; hardening recall for it is
    deferred to E6 (see the recall-stats-vs-nodes-touch question)."""
    ids = list(node_ids)
    if not ids:
        return
    await cur.execute(
        "UPDATE nodes SET recall_count = recall_count + 1, last_recalled_at = now() "
        "WHERE id = ANY(%s)",
        (ids,),
    )
