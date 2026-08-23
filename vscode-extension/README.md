# Engraphy for VS Code

**Associative memory for AI agents, modelled on the human mind.**

Bring the **Engraphy** memory engine into VS Code: register it as an MCP server
for Copilot / agent mode, **and** review and explore your memory directly in the
editor.

The name comes from *engraphy*, an old term from memory science for the process
of laying down an *engram*, the trace a memory leaves in the brain. Engraphy
does that for your agents: it checks each new memory against what it already
knows before the write lands, merging restatements, linking genuinely new facts,
and never silently overwriting. Nothing is deleted, so history stays walkable.

Engraphy is self-hosted: a typed knowledge graph on Postgres + pgvector with
embedding-native deduplication, hybrid retrieval, real graph traversal,
multi-principal spaces, and schema enforcement in the DB. It speaks MCP, and all
data stays on your own machine.

## Get started

Engraphy is self-hosted, so the extension needs a server to talk to. There is no
hosted option yet. Bringing one up locally takes two commands.

**Repo: <https://github.com/devon-clarkk/engraphy>**

### 1. Run a server

Requirements: **Docker** (with Compose). Nothing else.

```bash
git clone https://github.com/devon-clarkk/engraphy.git
cd engraphy

./up.sh          # writes a .env with random passwords, starts the stack,
                 # then blocks until /healthz returns 200
./provision.sh   # creates a space, applies the starter pack, mints a token,
                 # and prints the URL and token to paste in
```

On Windows use `.\up.ps1` and `.\provision.ps1`. Both scripts are safe to
re-run: an existing `.env` is never overwritten, and an existing space or an
already-applied pack is skipped rather than treated as an error.

`docker compose up -d` on its own is the equivalent one-liner. It is the whole
bring-up: an `init` sidecar runs the migrations and provisions the database role
after Postgres is healthy and before the server starts, so there is no separate
migrate step. You would then run the `engraphy-admin space create` /
`pack apply` / `token create` trio yourself; `provision` just does that and waits
for you.

First boot downloads the embedding model (~523 MB) into a volume before the
server answers anything. That happens once, and `up` waits it out.

### 2. Connect the extension

Run **Engraphy: Connect to a server** from the Command Palette and paste:

| | |
| --- | --- |
| Server URL | `http://127.0.0.1:8000/mcp/` (**keep the trailing slash**) |
| Token | the one `provision` printed |

The token is shown **once** and the server keeps only its SHA-256, so copy it
when it appears. Lost it? Re-run `provision` for a fresh one. The extension
stores it in your OS keychain, never in `settings.json`.

The extension then runs an authenticated read to confirm the pair works, so a
wrong token is reported as a wrong token rather than as a missing server. The
status bar turns green only after that read succeeds.

There is also a **Set up Engraphy** walkthrough inside the editor (Command
Palette: **Engraphy: Set up server / Getting Started**) covering the same ground
step by step, plus a manual-commands variant and what to check when the server
does not come up.

## Features

### MCP provider
Contributes `mcpServerDefinitionProviders` and registers a provider via
`vscode.lm.registerMcpServerDefinitionProvider` (VS Code 1.101+), so Engraphy's
tools appear in Copilot agent mode. Built from your settings; refreshes on change.

### Confirm-write queue + Memory explorer
An **Engraphy** activity-bar container with two views, driven by a typed MCP
client (Streamable HTTP + bearer token):

- **Confirm-write queue**, two bands:
  - **Inbox**: captured items awaiting triage (`inbox_review list`). Each has
    **Discard** (one click) and **Promote…**. Promotion is *authoring* by design
    (the server treats the captured payload as opaque), so Promote shows the raw
    payload read-only for reference and collects the node fields, and it never
    silently forwards the payload.
  - **Pending duplicates**: writes the server parked as `needs_confirmation`,
    listed by the server's `pending_list` tool. Each row shows the payload
    preview and the candidate it collided with; **Approve (keep distinct)** and
    **Deny (merge into…)**, a pick-list of the row's own candidates, are wired
    to `resolve_duplicate`. Expired rows (past `expires_at`) are **greyed and
    annotated**, not hidden; `resolve_duplicate` refuses them
    (`ENGRAPHY_PENDING_EXPIRED`), and the band footer counts them.
- **Memory explorer**: **Search memory…** → results → expand a node to traverse
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

## Connection reference

[Get started](#get-started) has the steps. This section is the detail behind them.

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
| `engraphy.serverUrl` | `http://127.0.0.1:8000/mcp/` | The Engraphy MCP endpoint. Keep the trailing slash, because `/mcp` 307-redirects to `/mcp/` and the round-trip adds ~10s/call (the extension normalizes a missing slash anyway). |
| `engraphy.token` | *(empty)* | **Deprecated.** Use **Engraphy: Connect to a server**; the token lives in the OS keychain. |
| `engraphy.space` | *(empty)* | Label only (real space comes from the token). |
| `engraphy.composeWorkingDirectory` | *(empty)* | Folder with `compose.yaml` + `.env`, for **Start local server**. |

### Connection status

The status bar reports one of: connected, token needed, token rejected,
unreachable, or no server set. It turns green only after an **authenticated**
read succeeds. `/healthz` is unauthenticated on an Engraphy server, so it
answers 200 for a server you hold no valid token for, and reporting health off
it alone would show a healthy bar over panels that cannot read anything.

## Building the extension

For contributors. Users need none of this.

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
from VSIX…** on the built `.vsix`. Connect it with **Engraphy: Connect to a
server** (or bring up a server first with **Engraphy: Start local server**), open
the Engraphy activity-bar view, and Refresh.

## License

Business Source License 1.1 (BUSL-1.1). See [LICENSE](LICENSE).
