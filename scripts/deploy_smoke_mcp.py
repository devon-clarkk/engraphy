"""Deploy smoke: a real MCP client round trip against a live Engraphy server.

The final step of the CI deploy-smoke job (.github/workflows/ci.yml). Everything
before it -- compose up, migrate, provision, space/pack/token -- is shell; this
is the part that proves the *product* works over the wire, not just that the
containers started.

Deliberately a pure over-the-wire client: it imports no engraphy code, connects by
Streamable HTTP with a bearer token exactly as an agent would, and drives
write -> paraphrase recall -> briefing -> both dedup bands (absorb and
merge-link). Mirrors the session wiring in engraphy/tests/test_app.py, but
against a real socket rather than an in-process ASGI transport.

Usage:
    ENGRAPHY_URL=http://127.0.0.1:8000 ENGRAPHY_TOKEN=<bearer> \
    ENGRAPHY_SCOPE=personal-<principal> python scripts/deploy_smoke_mcp.py

Exits 0 only if every assertion passes; prints a PASS/FAIL line for each so a CI
log shows exactly which leg broke.
"""
import asyncio
import contextlib
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

if sys.platform == "win32":  # parity with the rest of the codebase; CI runs Linux
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BASE = os.environ.get("ENGRAPHY_URL", "http://127.0.0.1:8000")
TOKEN = os.environ["ENGRAPHY_TOKEN"]
SCOPE = os.environ.get("ENGRAPHY_SCOPE", "personal-devon")

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}", flush=True)


@contextlib.asynccontextmanager
async def session():
    async with httpx.AsyncClient(
        base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120,
    ) as http_client, streamable_http_client(
        f"{BASE}/mcp/", http_client=http_client,
    ) as (read, write_stream, _session_id), ClientSession(read, write_stream) as s:
        await s.initialize()
        yield s


async def main() -> int:
    print(f"MCP deploy smoke against {BASE} (scope={SCOPE})")
    async with session() as s:
        tools = {t.name for t in (await s.list_tools()).tools}
        check("tools/list returns the core surface", {"write", "search", "briefing", "get"} <= tools,
              f"{len(tools)} tools")

        scopes = [x.get("id") for x in
                  ((await s.call_tool("scope_list", {})).structuredContent or {}).get("scopes", [])]
        check("scope_list exposes the target scope", SCOPE in scopes, f"scopes={scopes}")

        # --- write -------------------------------------------------------
        w1 = (await s.call_tool("write", {
            "scope": SCOPE, "type": "note",
            "title": "Deploy smoke: connection pooling decision",
            "body": ("We chose psycopg3's AsyncConnectionPool with a small min_size for the "
                     "Engraphy server, because per-request connect cost dominated the p50 "
                     "latency budget for search."),
            "attrs": {},
        })).structuredContent or {}
        node_id = (w1.get("node") or {}).get("id")
        check("write returns inserted", w1.get("outcome") == "inserted", f"outcome={w1.get('outcome')}")

        # --- recall (paraphrase: deliberately shares few words) -----------
        sr = (await s.call_tool("search", {
            "scope": SCOPE, "query": "why do we reuse database connections instead of opening them per call",
        })).structuredContent or {}
        hits = sr.get("results") or []
        hit = next((h for h in hits if (h.get("node") or {}).get("id") == node_id), None)
        check("search recalls the node by paraphrase", hit is not None,
              f"{len(hits)} result(s), similarity={hit and hit.get('similarity')}")

        # --- briefing -----------------------------------------------------
        b = (await s.call_tool("briefing", {"scope": SCOPE})).structuredContent or {}
        in_brief = any(n.get("id") == node_id
                       for sec in (b.get("sections") or []) for n in (sec.get("nodes") or []))
        check("briefing includes the node", in_brief,
              f"sections={[(x.get('name'), len(x.get('nodes') or [])) for x in (b.get('sections') or [])]}")

        # --- dedup, absorb band: a near-verbatim restatement is absorbed ---
        # Ordering matters. The absorb write runs BEFORE the merge-link write
        # because the novelty corpus is "canonical body + addenda + same_topic
        # peer bodies": once a peer exists, the Jaccard denominator grows and
        # the band a later write lands in stops being a property of this text
        # alone. Absorb first, and both bands are deterministic.
        w2 = (await s.call_tool("write", {
            "scope": SCOPE, "type": "note",
            "title": "Deploy smoke: connection pooling decision",
            "body": ("We chose psycopg3's AsyncConnectionPool with a small min_size for the "
                     "Engraphy service, because per-request connect cost dominated the p50 "
                     "latency budget for search."),
            "attrs": {},
        })).structuredContent or {}
        check("near-verbatim restatement is absorbed", w2.get("outcome") == "merged",
              f"outcome={w2.get('outcome')} similarity={w2.get('similarity')}")
        check("absorb reports the canonical it merged into",
              (w2.get("canonical") or {}).get("id") == node_id)

        # --- dedup, merge-link band: same topic, distinct wording ----------
        # A reworded note on the same topic is novel against the corpus, so the
        # write path keeps it as its own searchable node and joins it to the
        # canonical with a `same_topic` edge (docs/04-tools-reference.md).
        w3 = (await s.call_tool("write", {
            "scope": SCOPE, "type": "note",
            "title": "Deploy smoke: connection pooling decision",
            "body": ("We picked psycopg3's AsyncConnectionPool (small min_size) for the Engraphy "
                     "server since per-request connection cost dominated the search p50 latency "
                     "budget."),
            "attrs": {},
        })).structuredContent or {}
        member_id = (w3.get("node") or {}).get("id")
        check("reworded near-duplicate is kept and linked, never duplicated blindly",
              w3.get("outcome") == "merged_linked",
              f"outcome={w3.get('outcome')} similarity={w3.get('similarity')}")
        check("merge-link keeps the new wording as its own node",
              member_id is not None and member_id != node_id, f"member={member_id}")
        check("merge-link points at the same canonical",
              (w3.get("canonical") or {}).get("id") == node_id)

        # --- get: merge history top-level, never inside attrs -------------
        # `addenda` is a top-level wire key on every node (engraphy/core/get.py),
        # empty until something appends to it. The contract under test is the
        # SHAPE -- merge history is surfaced top-level and `attrs` never carries
        # it -- not a count: the only writer of an addendum is the error
        # re-occurrence path, and the starter pack this space is built from
        # declares no `error` type.
        g = (await s.call_tool("get", {"ids": [node_id]})).structuredContent or {}
        n0 = (g.get("nodes") or [{}])[0]
        check("get surfaces merge history top-level", isinstance(n0.get("addenda"), list),
              f"addenda={n0.get('addenda')!r}")
        check("attrs never carries addenda", "addenda" not in (n0.get("attrs") or {}))

        # The merge-link is traversable from the canonical: the edge is inserted
        # member -> canonical, so it reads as INBOUND here. Gated on the
        # envelope's own flag, which is false when the pack declares no
        # `same_topic` rule for the pair.
        if w3.get("cluster_edge_added"):
            inbound = (n0.get("edges") or {}).get("in") or []
            check("same_topic edge joins the member to the canonical",
                  any(e.get("type") == "same_topic" and e.get("src") == member_id
                      for e in inbound),
                  f"in={[(e.get('type'), e.get('src')) for e in inbound]}")

    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("deploy smoke OK -- write, recall, briefing and dedup all work over the wire.")
    return 0


sys.exit(asyncio.run(main()))
