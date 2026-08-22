# Example service units

Local/overlay profile only (design/04: cloud uses the Docker image + `compose.yaml`
at the repo root instead). Both units run `python -m engraphy.server.app`
directly against a venv at `/opt/engraphy/.venv` — adjust the paths if you
install elsewhere. See `deploy/checklist.md`'s local/overlay section for the
full install sequence these fit into.

- `engraphy.service` — systemd (Linux). Reads secrets from a separate
  `EnvironmentFile` (`/etc/engraphy/engraphy.env`, mode 0600) rather than
  inline, since unit files under `/etc/systemd/system` are world-readable.
- `com.engraphy.server.plist` — launchd (macOS, single-host deployments). launchd
  has no separate-file indirection for env vars, so secrets sit directly in
  this plist — keep the plist itself at 0600.

Postgres itself is not managed by either unit (design/04: "one Python
process + one Postgres per trust community" — Postgres is a prerequisite
service, installed/managed however your platform normally does that).
