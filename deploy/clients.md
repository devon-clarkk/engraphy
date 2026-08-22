# Client onboarding guide

Engraphy is pull/push, not ambient, by design (design/04: "judgment stays with
the agent"). What "onboard a client" means differs by client class — pick
the section that matches what you're connecting.

> **Validation status:** this doc is written from design/04 s.Client capture
> guidance and the mechanics below, but has not yet been proven by actually
> onboarding a hook-less client end to end — that's Phase E5's acceptance
> criterion ("Alex's onboarding used `deploy/clients.md` alone — gaps found
> become doc fixes, not tribal knowledge"). Treat this as the best current
> guidance, not a load-bearing guarantee; if something here doesn't work,
> that's exactly the kind of gap E5 is supposed to surface and fix.

## Before onboarding anyone: mint their token

Every client needs its own bearer token — tokens are per (space, principal,
client), never shared across devices (design/03: `client_name` becomes
`source_client` provenance, so mixing devices under one token loses that
signal). On the host, over the local `engraphy-admin` CLI (never over the
network — design/03: "no code path" for network admin):

```
engraphy-admin principal add --space <space> --id <principal> --display-name "..."   # first time only
engraphy-admin token create --space <space> --principal <principal> \
  --client-name <device-name> --role readwrite
```

`engraphy-admin` is a console script installed by `pip install .` into your
venv's `bin/`/`Scripts\` — it is only on PATH while that venv is **activated**.
If you get `command not found`, either activate the venv or use the always-works
form from the repo root:

```
python -m engraphy.admin.cli token create --space <space> ...
```

On the **cloud/compose profile** you don't need either on the host — run it in
the sidecar (which reaches postgres over the compose network):

```
docker compose --profile admin run --rm admin \
  engraphy-admin token create --space <space> --principal <principal> \
    --client-name <device-name> --role readwrite
```

The token prints exactly once. Put it straight into the client's MCP config
(below) — it cannot be retrieved again; `token revoke` + `token create` a
new one if it's lost.

## Plan prerequisites (check this first, per client)

Not every Claude plan/surface can add a remote MCP connector. Verify this
**before** promising someone their memory works on a given device — a plan
limitation here is a hard blocker, not a config issue:

- Claude Code: remote MCP servers are supported on all plans that run Claude
  Code at all.
- Claude Desktop: remote (Streamable HTTP) MCP connectors require a paid
  plan tier; check the specific person's plan against Anthropic's current
  connector documentation before onboarding, since this changes over time
  and is out of this repo's control to pin.
- Claude mobile (iOS/Android): connector support tracks Desktop's, generally
  a step behind on rollout — verify per app version.

If a client's plan can't add a remote connector, they cannot reach this
server from that surface at all — no engraphy-side workaround exists (it isn't
transport-agnostic in that direction; MCP-over-Streamable-HTTP is the only
wire protocol this server speaks, per design/03).

## Hook-capable harnesses (Claude Code, and future harnesses with hook support)

The exemplar pattern:

1. **Session-start briefing injection.** A hook fires on session start,
   calls the `briefing` tool, and injects the result into context — the
   agent starts every session already knowing what it should. No slash
   command or manual step from the human.
2. **Auto-capture failures to `/inbox`.** A hook on task/session end (or on
   an explicit failure signal your harness exposes) POSTs to `/inbox`
   (design/03/04: bearer-authed, same token as MCP) rather than requiring
   the agent to remember to call `write` mid-task. Inbox items land
   `status='pending'`, reviewed later via `inbox_review` — this is the
   "auto-captured noise becoming memory without judgment" guard design/04
   calls out: capture is cheap and automatic, promotion to real memory is
   not.
3. Add the MCP server to the harness's config pointing at `/mcp` on your
   deployment's bind address with the bearer token from above. Claude Code:
   project or user `mcp` config, `"type": "http"`, `"url": "https://.../mcp"`,
   `"headers": {"Authorization": "Bearer <token>"}`.

## Hook-less clients (Claude Desktop, Claude mobile — no hook surface)

No session-start injection and no auto-capture are possible here — the tool
descriptions themselves have to carry the whole protocol, and the human (or
a scheduled job) has to trigger writes explicitly:

1. **Connect the MCP server** the same way as above (`/mcp`, bearer token) —
   the connector UI differs by client but the URL + token pair is identical.
2. **The tool descriptions do the teaching.** Pack `tool_descriptions`
   overrides (design/03: "engine base text + pack tool_descriptions
   overrides") should explicitly instruct the model: "call `briefing` at the
   start of a conversation", "when the user states a lasting preference or
   fact, call `write`". This is the only place instructions reach a
   hook-less client's model — there's no other injection point.
3. **Scheduled recap, for people (not agents) who won't manually invoke
   `write`.** Set up a recurring prompt — a vendor scheduled task/reminder,
   or a custom scheduled runner if you have one — that runs on a cadence (daily is
   the documented reference point) and reviews the period's conversation,
   writing distilled nodes. This is safe to run **even if it partially
   repeats a previous recap**, because dedup absorbs re-tellings (design/04:
   "Engraphy's write path is idempotent-ish by construction" — a near-duplicate
   write merges into the existing node rather than creating drift). That's
   what makes this viable for someone who won't remember to run it
   consistently: missing a day, or double-running one, doesn't rot the graph
   the way it would against a server without dedup.
4. There is no equivalent of hook-based `/inbox` auto-capture here. If the
   client surface later gains any hook/automation capability, migrate it to
   the hook-capable pattern above rather than keeping the scheduled-recap
   workaround.

## Verifying onboarding worked

Regardless of client class, confirm before considering someone onboarded:

- `scope_list` returns at least one scope (proves the token resolves). On a
  fresh install the only writable scope is `personal-<principal>` — the pack
  ships node *types*, not scopes.
- `briefing` returns a non-error result (proves the token resolves and the
  space/scope exist).
- A `write` round-trips: write a throwaway node, `get` it back by id, then
  archive or leave it (there are no hard deletes — design/01).
- A `search` **recalls it by paraphrase** — the real proof, since persistence
  alone isn't the product. Note the envelope shape: hits come back under
  `results`, each wrapping the node (**not** a bare `nodes` list):

  ```json
  {
    "v": 1,
    "detail": "full",
    "results": [
      {
        "node": {
          "id": "82dba4dc-...",
          "type": "note",
          "scope": "personal-devon",
          "title": "Postgres connection pooling decision",
          "body": "...",
          "attrs": {},
          "status": "active",
          "author": "devon",
          "created_at": "2026-07-20T09:55:20Z"
        },
        "score": 0.016393,
        "similarity": 0.8,
        "edge_count": 0
      }
    ],
    "scopes_searched": ["personal-devon"],
    "truncated": false
  }
  ```

  (`score` is the fused RRF rank, `similarity` the raw vector cosine. A
  paraphrase that shares no keywords should still return the node with a
  `similarity` well above the noise floor.)
- For hook-capable harnesses: trigger the failure-capture path once
  deliberately and confirm the item lands in `/inbox` (`inbox_review` shows
  it `status='pending'`).
