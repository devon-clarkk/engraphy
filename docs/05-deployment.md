# Deployment Guide

Engraphy ships two supported deployment postures from the same artifact. Pick one.

| Profile | Placement | Transport | Install |
|---|---|---|---|
| **Local / overlay** | home server, reachable only over a tailnet/VPN | the overlay network *is* the encryption; TLS optional | systemd (Linux) or launchd (macOS) unit — `deploy/units/` |
| **Cloud** | public VM/container host | **TLS mandatory** (reverse proxy in front) | Docker image + `compose.yaml` |

The full step-by-step for both lives in **`deploy/checklist.md`** (follow it in
order — each step depends on the one before). This guide is the operational
overview and the reference for auth, scopes, and day-2 tasks.

## Environment variables

| Var | Used by | Meaning |
|---|---|---|
| `ENGRAPHY_DATABASE_URL` | server (app role) and admin CLI (superuser) | Postgres connection. The **server** uses the non-superuser `engram_app` role; **admin/migrate** use the superuser/owner role. |
| `ENGRAPHY_BIND_HOST` | server | interface to bind (default `127.0.0.1`). |
| `ENGRAPHY_BIND_PORT` | server | port (default `8000`). |
| `ENGRAPHY_INSECURE_TRANSPORT_OK` | server | set `true` **only** to bind a genuinely public interface without TLS on an overlay-only network. |
| `ENGRAPHY_LAST_BACKUP_STATUS_FILE` | server | path whose contents `/healthz` reports as `last_backup_at` — your backup job writes the completion timestamp here. |

## Running as a service

### Local / overlay (systemd / launchd)

1. Bind an **overlay-only** address (a tailnet `100.x.y.z`, or `127.0.0.1` behind a
   local proxy) — never `0.0.0.0` or a bare public interface.
2. Provision the app role once: `psql "$SUPERUSER_URL" -v
   app_role_password=… -f deploy/provision-app-role.sql`.
3. `engraphy-admin migrate --dump-dir /var/backups/engraphy` (superuser URL).
4. Install the unit from `deploy/units/` (`engraphy.service` for Linux,
   `com.engraphy.server.plist` for macOS), put the env vars in a mode-0600
   `/etc/engraphy/engraphy.env`, and `systemctl enable --now engraphy` (or `launchctl
   load …`).

The server refuses to bind a *public* interface in plaintext unless
`ENGRAPHY_INSECURE_TRANSPORT_OK=true`; loopback / RFC1918 / CGNAT / tailnet ranges are
exempt and need no opt-in.

### Cloud (Docker + reverse proxy)

`compose.yaml` defines three services: **postgres** (`pgvector/pgvector:pg16`),
**engraphy** (the server), and a one-shot **admin** sidecar (carries `dbmate` +
`pg_dump`/`pg_restore`/`psql`, which the server image omits).

1. Create a git-ignored `.env` next to `compose.yaml` with `POSTGRES_PASSWORD` and
   `ENGRAPHY_APP_ROLE_PASSWORD`.
2. `docker compose up -d postgres`; wait for `(healthy)`.
3. Migrate via the sidecar (it reaches postgres over the compose network, so
   postgres needs no published port):
   ```bash
   docker compose --profile admin run --rm admin engraphy-admin migrate --dump-dir /backups
   ```
4. Provision the app role via the sidecar:
   ```bash
   docker compose --profile admin run --rm admin \
     psql "$ENGRAPHY_DATABASE_URL" -v app_role_password="$ENGRAPHY_APP_ROLE_PASSWORD" \
     -f deploy/provision-app-role.sql
   ```
5. Put a TLS-terminating reverse proxy (Caddy/nginx/Traefik or a load balancer) in
   front of `127.0.0.1:8000` — engraphy's own port is bound to loopback and never
   exposed directly. Caddy one-liner: `your.domain { reverse_proxy 127.0.0.1:8000 }`.
6. `docker compose up -d engraphy`. **First boot downloads the ~523 MB embedding
   model** into the `model-cache` volume before serving — watch `docker compose ps`
   go `(health: starting)` → `(healthy)`. `Restarting` means a real error, not a
   slow download; read `docker compose logs engraphy`.

> The transport-security refusal classifies the *bind host* — a container bound to
> `0.0.0.0` is classified "private" and boots without the opt-in even though a
> published port makes it reachable. The real boundary is `compose.yaml`'s
> loopback `ports:` mapping + your firewall, not this check. Only 443 (your proxy)
> should be open to the internet; engraphy's and postgres's ports must not be.

## Auth, principals, scopes

- **Tokens** are per `(space, principal, client_name)`, display-once bearer tokens
  (only a SHA-256 is stored). `client_name` becomes the `source_client` provenance
  on every write, so never share a token across devices. Mint with `engraphy-admin
  token create …`; revoke with `engraphy-admin token revoke …` (effective on the
  next request — no cache window). A `role` of `readwrite` or `readonly` gates
  write tools.
- **Principals** are actors in a space. Each CLI-created principal gets a private,
  ambient `personal-<id>` scope in the same transaction.
- **Scopes** are isolation containers with a `visibility`:
  - `private` — owner + explicit grants only.
  - `team-read` — every principal in the space may read.
  - `team-write` — every principal may read and write.
  Change with `admin_scope_visibility`; grant a specific principal access with
  `admin_grant` (`read`/`write`). All of this is enforced by Postgres RLS — the app
  role is not `BYPASSRLS`, so a bug in the application layer cannot leak across
  scopes.
- **Space administration is CLI-only for bootstrap.** `space create` and the first
  token must come from `engraphy-admin` on the host (there is no network code path).
  Ongoing member/token/visibility/grant management is available *either* via the
  CLI *or* via the four `admin_* ` MCP tools — unless a space sets
  `space_admin_tools: false` (via `engraphy-admin config set`), which removes those
  tools entirely for the paranoid "everything via the CLI" posture.

## Day-2 operations (the `engraphy-admin` CLI)

Run against the **superuser** connection. All verbs also work as `python -m
engraphy.admin.cli <verb>` if `engraphy-admin` isn't on `PATH`.

| Command | What it does |
|---|---|
| `space create --id … --display-name … --principal …` | create a space + founding `space_admin` + personal scope + restore sentinel. |
| `principal add --space … --id … --display-name … [--role …]` | add a member (+ their personal scope). |
| `token create / token revoke` | mint / revoke a bearer token. |
| `config set --space … --key … --value …` | set a per-space config value (JSON), e.g. `dedup.t_high`, `space_admin_tools`. |
| `pack validate / pack apply / pack upgrade` | validate, apply, or migrate a pack's ontology. |
| `import <file.jsonl> --space … --scope … --principal …` | bulk-load through the dedup pipeline (idempotent; pending items → review-queue CSV). |
| `addenda promote --space …` | promote get-only addenda into searchable member nodes (Phase B recovery). |
| `surface rebuild --space …` | recompute the searchable attr surface + re-embed changed rows (run after a Phase C migration or a `searchable`-flag change). |
| `migrate` | pre-dump → `dbmate up` → restart → smoke test. |
| `verify-restore --against <dump>` | restore a dump into a throwaway scratch DB and assert schema version, row counts, constraints, and sentinel retrieval. Safe against production's own connection (never touches the live DB). |
| `doctor --space …` | read-only hygiene report (stale pendings, nonconforming attrs, registry drift, long chains, over-addenda nodes). |

## Backups & restore-testing

Backups are the operator's job (Engraphy takes an unconditional pre-dump before
every `migrate`, but ongoing backups are external):

1. Schedule `pg_dump --format=custom` on your RPO cadence (6 h is the documented
   personal-scale sweet spot).
2. Have the job write the completion timestamp to `ENGRAPHY_LAST_BACKUP_STATUS_FILE`,
   so `/healthz` surfaces staleness in-band.
3. **Monthly**, run `engraphy-admin verify-restore --against <one of the dumps>` —
   "restore-tested, not just taken". It asserts the restore is usable, including
   retrieving the space's restore **sentinel** node.

## Health & version

`GET /healthz` (unauthenticated) returns `{status, version, schema_version, spaces,
embedding_model, last_backup_at?}`. `schema_version` should equal the
highest-numbered file in `engraphy/db/migrations/`; the server **refuses to start** if
the DB is behind (run `engraphy-admin migrate`).

## Client onboarding

Point an MCP client at `https://your.domain/mcp` (or the overlay address for the
local profile) with `Authorization: Bearer <token>`. Per-client configuration
(Claude Code, Claude Desktop, others) and plan prerequisites are in
`deploy/clients.md`.
