# Change Log

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
