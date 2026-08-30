# Change Log

## 0.6.0 (unreleased)

### The indicator reports end-to-end capability

The status bar answers the question it is read for: can an agent use Engraphy
right now. That needs both a server this extension can read and an agent that
holds the tools, and it reports ready only when both hold. A reachable server
whose tools no agent holds reads **Engraphy: agent cannot see memory**, and its
tooltip names which of the two is missing and why.

The extension records every time VS Code asks for its MCP server definitions.
Being asked is the evidence that the editor consumed the registry, and it
separates three states that previously looked identical: a VS Code too old for
the provider API, a provider registered and never queried, and a provider
queried and answered.

When the provider is registered and VS Code has never queried it, the tooltip
reports that as the observation it is, then names the causes. It reads
`chat.mcp.enabled`, `chat.mcp.access`, `chat.mcp.allowManagedServersOnly` and
`chat.mcp.deniedServers`, so an editor where MCP is restricted says so and
points at the setting, including where an organisation sets it.

Each installed agent is judged on its own. An editor where one agent holds the
tools and another installed one does not says so by name, rather than letting a
single working path stand in for the rest. An agent you do not use can be
dismissed, and registering it later brings it back.

Because the status bar can be hidden, these states also raise a notification
with a one-click next step. It appears once, and arms again if a working setup
later breaks.

### Writes are checkable against the server

**Engraphy: Check that writes are reaching the server** reports when memory
last actually changed, read from the server's own counters. An agent can only
report a save it made, so this is the answer that does not depend on what the
agent says. It reads the metrics rollup and records no usage of its own, so
checking never moves the numbers in the Impact & usage panel.

### Engraphy registers with agents that read their own config

Copilot agent mode receives the server through VS Code's MCP provider API.
Claude Code and Cursor read their own MCP config instead, so
**Engraphy: Register with your coding agent** writes the entry there and reads
the file back to confirm it landed before reporting success. It takes a
timestamped backup, writes through a temp file and a rename, abandons the write
if the agent changed the file underneath it, and leaves a config carrying
comments or invalid JSON untouched. Writing a token into an agent's plaintext
config is confirmed with you first; the keychain copy is unchanged.

## 0.5.2

Setup and onboarding content only. No code, dependency or behaviour change from
0.5.1, so this build supersedes it for upload.

The Marketplace page had no getting-started path at all. Its first actionable
heading was "Connecting", which assumed you already had a server running. A
**Get started** section now sits above Features with the two-command local
bring-up and the connect steps, and it names the repo rather than assuming you
can find it.

The Docker walkthrough step had drifted from how the stack actually starts. It
told you to bring up Postgres, run `engraphy-admin migrate`, and apply
`provision-app-role.sql` by hand. An `init` sidecar has done all three since the
one-command bring-up landed, and the server is gated on that sidecar exiting
cleanly, so the manual sequence was both redundant and a path the compose file no
longer models. One of its commands could not have worked as printed: it referred
to `$ENGRAPHY_DATABASE_URL`, which is set inside the admin container but expands
to nothing in the shell you paste into. The step is now `git clone`, then `up`
and `provision` (with the PowerShell variants named), with the by-hand commands
kept below as a corrected alternative.

The walkthrough also told you to get "a checkout of the Engraphy repo" without
ever saying where from. Both it and the welcome step now link
<https://github.com/devon-clarkk/engraphy>.

Separately, the repo's `provision` scripts printed client settings that told you
to paste your token into `engraphy.token`, the setting 0.5.0 deprecated. They now
point at the **Engraphy: Connect to a server** command and note that the token
goes to the OS keychain.

## 0.5.1

Marketplace listing copy only. No code, dependency or behaviour change from
0.5.0, so this build supersedes it for upload.

The listing now leads with the product's tagline, "Associative memory for AI
agents, modelled on the human mind", and the README explains where the name
comes from: engraphy, an old term from memory science for the process of laying
down an engram, the trace a memory leaves in the brain.

## 0.5.0

First public release. Everything below is a cold-start correctness fix: each one
is something a new install hit in its first minute.

### Your token now lives in the OS keychain

`engraphy.token` was an ordinary setting, which meant a live credential sat in
plain text in `settings.json`, showed up in the Settings UI, and was eligible for
Settings Sync. It is now stored through VS Code's SecretStorage. An existing
setting is migrated into the keychain and cleared on first activation, and the
setting is marked deprecated. Set the token with **Engraphy: Connect to a
server**.

### A rejected token no longer reads as a missing server

A 401 was classified the same as a connection failure, so a healthy server that
refused your token told you to go start a server that was already running.
Connection failures are now split three ways, with a different remedy for each:

- **no server set** offers setup,
- **unreachable** offers retry, reconnect, and a URL change,
- **token rejected** offers a token update and says plainly that the server is
  up and this is a credentials problem.

"The server needs a token" and "the server refused your token" are also worded
differently, because telling someone their token was rejected when they never
entered one sends them hunting for a fault that is not there.

### The status bar can no longer be green over failing panels

It reported health from `/healthz`, which is unauthenticated and answers 200 for
a server you cannot read a byte from. It now requires an authenticated
`scope_list` read before reporting connected, and shows token-needed,
token-rejected, unreachable, or server-error otherwise. `scope_list` is used
because it records no usage metrics, so polling cannot inflate the counters the
Impact & usage panel reports.

### The brand mark renders

`media/loop-mark.svg` named a CSS custom property inside an XML comment, leading
hyphens included. An XML comment may not contain a double hyphen, so the file was
not well-formed, browsers refused it as an image, and a CSS mask whose image
fails to load paints nothing and logs nothing. The header mark on both panels and
every large mark in the onboarding screen rendered blank. The comment is fixed,
and the test suite now fails on a malformed comment in any shipped SVG.

### Concurrent connects no longer leak MCP sessions

`ensureConnected` had no in-flight guard, so the callers that fire together at
startup each built a transport and the losers were orphaned instead of closed.
Concurrent callers now share one connect, keyed so a settings change mid-connect
starts a fresh one rather than handing back a client built with the old token.

### Panel states

Both panels gained first-load skeletons, real empty states, and inline
per-band errors with their own retry, so nothing looks broken while data is on
its way. An all-zero brand-new space now says nothing has been recorded yet
instead of rendering a wall of zeroes.

### Not in this release

The graph explorer. It is planned for 0.6.0.

## 0.4.0

Confirm-write queue and memory explorer as branded webviews, the Impact & usage
panel, the setup walkthrough, and MCP server registration with VS Code.
