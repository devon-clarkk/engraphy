"""Deploy smoke: a real MCP client round trip against a live Engraphy server.

The final step of the CI deploy-smoke job (.github/workflows/ci.yml). Everything
before it -- compose up, migrate, provision, space/pack/token -- is shell; this
is the part that proves the *product* works over the wire, not just that the
containers started.

Deliberately a pure over-the-wire client: it imports no engraphy code, connects by
Streamable HTTP with a bearer token exactly as an agent would, and drives
write -> paraphrase recall -> briefing -> near-duplicate merge. Mirrors the
session wiring in engraphy/tests/test_app.py, but against a real socket rather
than an in-process ASGI transport.

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
    ) as http_client:
        async with streamable_http_client(
            f"{BASE}/mcp/", http_client=http_client,
        ) as (read, write_stream, _session_id):
            async with ClientSession(read, write_stream) as s:
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

        # --- dedup: near-duplicate must merge, not duplicate --------------
        w2 = (await s.call_tool("write", {
            "scope": SCOPE, "type": "note",
            "title": "Deploy smoke: connection pooling decision",
            "body": ("We picked psycopg3's AsyncConnectionPool (small min_size) for the Engraphy "
                     "server since per-request connection cost dominated the search p50 latency "
                     "budget."),
            "attrs": {},
        })).structuredContent or {}
        check("near-duplicate merges instead of duplicating", w2.get("outcome") == "merged",
              f"outcome={w2.get('outcome')} similarity={w2.get('similarity')}")
        check("merge targets the same canonical node",
              (w2.get("canonical") or {}).get("id") == node_id)

        # --- get: merge history top-level, never inside attrs -------------
        g = (await s.call_tool("get", {"ids": [node_id]})).structuredContent or {}
        n0 = (g.get("nodes") or [{}])[0]
        check("get surfaces merge history top-level", len(n0.get("addenda") or []) >= 1,
              f"{len(n0.get('addenda') or [])} addendum/a")
        check("attrs never carries addenda", "addenda" not in (n0.get("attrs") or {}))

    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("deploy smoke OK -- write, recall, briefing and dedup all work over the wire.")
    return 0


sys.exit(asyncio.run(main()))
