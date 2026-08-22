# I already have a server

If you (or your team) already run an Engraphy server, you just need its **MCP URL**
and a **token**.

Run **Engraphy: Connect to a server (set URL + token)…** (the button on the left,
or the Command Palette). You'll be asked for:

- **Server URL** — the MCP endpoint, e.g. `http://127.0.0.1:8000/mcp/` for a
  local bring-up, or `https://your-host.example/mcp/` behind your TLS proxy.
  **Keep the trailing slash** — `/mcp` 307-redirects to `/mcp/` and the
  round-trip adds ~10s per call (the extension normalizes a missing slash
  anyway, but the default is correct as shipped).
- **Token** — a bearer token from `engraphy-admin token create`. **The token is
  your identity** — it resolves to a (space, principal, role) on the server.
  Leave the field blank to keep the token you already have set.

The URL is saved to your **User Settings** (`engraphy.serverUrl`). The **token is
stored in your OS keychain**, not in `settings.json`, so it is never written in
plain text and Settings Sync never carries it.

The extension then runs an **authenticated** read to confirm the pair actually
works, and tells you which of the two is wrong if it does not. You can re-run it
any time to switch servers.

> Prefer editing settings directly? `engraphy.serverUrl` and `engraphy.space` are
> plain settings, so the Settings UI or `settings.json` both work. The token is
> not: use this command, so it lands in the keychain.
