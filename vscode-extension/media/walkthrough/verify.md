# Verify the connection

Once a server URL and token are set, confirm it's live:

- Run **Engraphy: Reconnect** (the button on the left). It reconnects the MCP
  client and refreshes the panels, then reports "reconnected and refreshed".
- Watch the **status bar** (bottom-left): `↻ Engraphy` means the server is
  reachable; `⚠ Engraphy` means it isn't (hover for the reason).
- Open the **Engraphy** icon in the Activity Bar. When connected, the
  **Confirm-write queue** shows your pending duplicates and inbox instead of the
  "No server connected" screen.

### If it still says "No server connected"

- **Server not running?** Go back to **Run locally with Docker** and check
  `docker compose ps`; engraphy should be `healthy`.
- **Wrong URL?** It should end in `/mcp/`. Re-run **Connect to a server…**.
- **Auth error?** The token may be missing, wrong, or for a different space;
  mint a fresh one with `engraphy-admin token create` and paste it in.
- Check the **Engraphy** output channel (View → Output → "Engraphy") for the
  exact error.

That's it, you're set up. Writes your agents make that collide with existing
memories will land in the confirm-write queue for you to approve or merge.
