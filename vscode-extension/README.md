# Engraphy for VS Code

Bring the **Engraphy** memory engine into VS Code: register it as an MCP server
for Copilot / agent mode, **and** review and explore your memory directly in the
editor.

Engraphy is a self-hosted memory engine for AI agents — a typed knowledge graph
on Postgres + pgvector with embedding-native deduplication, hybrid retrieval,
real graph traversal, multi-principal spaces, and schema enforcement in the DB.

## Features

### MCP provider
Contributes `mcpServerDefinitionProviders` and registers a provider via
`vscode.lm.registerMcpServerDefinitionProvider` (VS Code 1.101+), so Engraphy's
tools appear in Copilot agent mode. Built from your settings; refreshes on change.

### Confirm-write queue + Memory explorer
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
- **Status bar**: requires an authenticated read before it reports connected;
  see [Connection status](#connection-status).
- **Refresh** command on both views.

## Pending-duplicate listing

The Pending duplicates band reads the server's read-only **`pending_list`** tool
(`{limit?, offset?}` → `{v:1, pending:[{id, payload_preview, candidates:[{id,
title, similarity}], expires_at, created_at}]}`). `payload_preview` is a plain
string (title plus a capped body) rendered as-is. A manual **Resolve pending
duplicate by id…** command is also available for a `pending_id` obtained
elsewhere.

`pending_list` deliberately does **not** filter expired rows, so that staleness
stays visible in the client. It requires an Engraphy server at 0.1.0 or newer.

## Connecting

Run **Engraphy: Connect to a server** from the command palette. It asks for the
MCP URL and your token, saves them, and then validates the connection with an
authenticated read, so a wrong token is reported as a wrong token rather than as
a missing server.

### Where your token is kept

Your bearer token **is** your identity on an Engraphy server: it resolves to a
(space, principal, role). It is stored in the OS keychain through VS Code's
SecretStorage, not in `settings.json`, so it is never written in plain text and
is never carried by Settings Sync.

The old `engraphy.token` setting is deprecated. If you have a value there from
an earlier version, it is moved into the keychain and cleared from your settings
the first time 0.5.0 activates.

### Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `engraphy.serverUrl` | `http://127.0.0.1:8000/mcp/` | The Engraphy MCP endpoint. Keep the trailing slash — `/mcp` 307-redirects to `/mcp/` and the round-trip adds ~10s/call (the extension normalizes a missing slash anyway). |
| `engraphy.token` | *(empty)* | **Deprecated.** Use **Engraphy: Connect to a server**; the token lives in the OS keychain. |
| `engraphy.space` | *(empty)* | Label only (real space comes from the token). |
| `engraphy.composeWorkingDirectory` | *(empty)* | Folder with `compose.yaml` + `.env`, for **Start local server**. |

### Connection status

The status bar reports one of: connected, token needed, token rejected,
unreachable, or no server set. It turns green only after an **authenticated**
read succeeds. `/healthz` is unauthenticated on an Engraphy server, so it
answers 200 for a server you hold no valid token for, and reporting health off
it alone would show a healthy bar over panels that cannot read anything.

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

## License

Business Source License 1.1 (BUSL-1.1). See [LICENSE](LICENSE).
