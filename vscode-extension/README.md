# Engraphy for VS Code

Bring the **Engraphy** memory engine into VS Code: register it as an MCP server
for Copilot / agent mode, **and** review and explore your memory directly in the
editor.

Engraphy is a self-hosted memory engine for AI agents — a typed knowledge graph
on Postgres + pgvector with embedding-native deduplication, hybrid retrieval,
real graph traversal, multi-principal spaces, and schema enforcement in the DB.

## Features

### Tier 1 — MCP provider (unchanged)
Contributes `mcpServerDefinitionProviders` and registers a provider via
`vscode.lm.registerMcpServerDefinitionProvider` (VS Code 1.101+), so Engraphy's
tools appear in Copilot agent mode. Built from your settings; refreshes on change.

### Tier 2 — Confirm-write queue + Memory explorer
An **Engraphy** activity-bar container with two views, driven by a typed MCP
client (Streamable HTTP + bearer token):

- **Confirm-write queue** — two bands:
  - **Inbox** — captured items awaiting triage (`inbox_review list`). Each has
    **Discard** (one click) and **Promote…**. Promotion is *authoring* by design
    (the server treats the captured payload as opaque), so Promote shows the raw
    payload read-only for reference and collects the node fields — it never
    silently forwards the payload.
  - **Pending duplicates** — writes the server parked as `needs_confirmation`,
    listed by the server's `pending_list` tool. Each row shows the payload
    preview and the candidate it collided with; **Approve (keep distinct)** and
    **Deny (merge into…)** — a pick-list of the row's own candidates — are wired
    to `resolve_duplicate`. Expired rows (past `expires_at`) are **greyed and
    annotated**, not hidden; `resolve_duplicate` refuses them
    (`ENGRAPHY_PENDING_EXPIRED`), and the band footer counts them.
- **Memory explorer** — **Search memory…** → results → expand a node to traverse
  to its linked neighbors (`search` / `traverse` / `get`). Click a node to open
  its full JSON.
- **Status bar** — polls `/healthz`; shows reachable / offline with the space.
- **Refresh** command on both views.

## Pending-duplicate listing (server `pending_list`)

Earlier the server had no way to list outstanding pending duplicates, so this
band was a placeholder. The server now ships a read-only **`pending_list`** tool
(`{limit?, offset?}` → `{v:1, pending:[{id, payload_preview, candidates:[{id,
title, similarity}], expires_at, created_at}]}`) and the band is wired to it
directly. `payload_preview` is a plain string (title + capped body) rendered
as-is. A manual **Resolve pending duplicate by id…** command remains for a
`pending_id` obtained elsewhere.

`pending_list` deliberately does **not** filter expired rows — it surfaces
staleness so the client can show it. Requires a server built from a branch that
includes `pending_list` (e.g. `feature/pending-list`).

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `engraphy.serverUrl` | `http://127.0.0.1:8000/mcp/` | The Engraphy MCP endpoint. Keep the trailing slash — `/mcp` 307-redirects to `/mcp/` and the round-trip adds ~10s/call (the extension normalizes a missing slash anyway). |
| `engraphy.token` | *(empty)* | Bearer token; **it is your identity**. |
| `engraphy.space` | *(empty)* | Label only (real space comes from the token). |
| `engraphy.composeWorkingDirectory` | *(empty)* | Folder with `compose.yaml` + `.env`, for **Start local server**. |

## Build

```bash
npm install
npm run check-types    # tsc --noEmit
npm run test-client    # unit tests for result parsing + arg building
npm run compile        # esbuild bundle -> out/extension.js
npx @vscode/vsce package --no-dependencies   # -> engraphy-<version>.vsix
```

The extension is **bundled** with esbuild (the MCP SDK is inlined), so no
`node_modules` ships in the `.vsix`.

## Load / test locally

Press <kbd>F5</kbd> for an Extension Development Host, or **Extensions: Install
from VSIX…** on the built `.vsix`. Point `engraphy.serverUrl` / `engraphy.token`
at a running Engraphy server (or use **Engraphy: Start local server**), open the
Engraphy activity-bar view, and Refresh.

> Live UI + server round-trips (MCP handshake, tool calls) can only be verified
> against a running VS Code + live Engraphy server. Headlessly verified here:
> type-check, unit tests, bundle, and manifest.

## Publishing (Devon)

Create a Marketplace publisher, swap the placeholder `publisher: "engraphy"`,
generate a PAT, and run `vsce publish`. (Not published here — no PAT.)
