# Setup & Install

This guide brings Engraphy up **locally, end-to-end**: Postgres + pgvector, the
schema, a space with a pack, a token, and the running MCP server — enough to write
your first memory and search it back. For production postures (systemd/launchd or
Docker + reverse proxy), see the [Deployment guide](05-deployment.md).

> Throughout, the code, CLI, and env vars are named `engraphy` (the product is
> Engraphy). Prefer `127.0.0.1` over `localhost` in connection URLs — on IPv6-first
> systems `localhost` adds a ~10 s timeout per connection.

## 0. Prerequisites

- **Postgres 16 with the `pgvector` extension.** The `pgvector/pgvector:pg16`
  image ships both; a native install needs `CREATE EXTENSION vector` to succeed.
- **Python ≥ 3.12** and a virtualenv.
- **[dbmate](https://github.com/amacneil/dbmate)** on `PATH` — `engraphy-admin
  migrate` shells out to it.
- Postgres client tools (`psql`, and `pg_dump`/`pg_restore` if you'll take backups
  or run `verify-restore`).
- On first server boot the embedding model `nomic-ai/nomic-embed-text-v1.5`
  (~523 MB) downloads and is cached.

## 1. Start Postgres + pgvector

The quickest local database is the pgvector image with a published loopback port:

```bash
docker run -d --name engraphy-pg \
  -e POSTGRES_PASSWORD=devpw -e POSTGRES_DB=engraphy \
  -p 127.0.0.1:5432:5432 \
  pgvector/pgvector:pg16
```

Your **superuser** URL (used for migrations and admin) is then:

```
postgres://postgres:devpw@127.0.0.1:5432/engram?sslmode=disable
```

## 2. Install the package

```bash
python -m venv .venv && source .venv/bin/activate    # .venv\Scripts\activate on Windows
pip install .            # or  pip install .[dev]  to also run the test suite
```

This installs the `engraphy-admin` console script into the venv. It is on `PATH`
only while the venv is **activated**; if you ever get `command not found`, either
re-activate or use the always-works form from the repo root:

```bash
python -m engraphy.admin.cli <verb> ...
```

## 3. Provision the application role

Engraphy runs the server as a **non-superuser** role (`engram_app`) so that
Row-Level Security actually constrains it — the app role is deliberately **not**
`BYPASSRLS` and cannot run migrations. Create it once, against the superuser
connection, using the shipped script:

```bash
psql "postgres://postgres:devpw@127.0.0.1:5432/engram?sslmode=disable" \
  -v app_role_password="devapppw" \
  -f deploy/provision-app-role.sql
```

Your **app-role** URL (used by the running server) is then:

```
postgres://engram_app:devapppw@127.0.0.1:5432/engram?sslmode=disable
```

## 4. Run the migrations

`engraphy-admin migrate` wraps the safe sequence — an unconditional pre-migration
`pg_dump`, then `dbmate up`, then an optional restart + smoke test. On a brand-new
DB the pre-dump is nearly empty and there is nothing to restart yet, which is fine.
Run it against the **superuser** URL:

```bash
ENGRAPHY_DATABASE_URL="postgres://postgres:devpw@127.0.0.1:5432/engram?sslmode=disable" \
  engraphy-admin migrate --dump-dir ./backups
```

Under the hood this runs `dbmate --migrations-dir engraphy/db/migrations
--no-dump-schema --url <conninfo> up`. The migrations ship as package data, so the
`--migrations-dir` default resolves even from an installed (non-repo) location.

## 5. Create a space, apply a pack, mint a token

Space/principal/token administration is **local-CLI only** (there is deliberately
no network code path for it). Run these against the **superuser** URL:

```bash
export ENGRAPHY_DATABASE_URL="postgres://postgres:devpw@127.0.0.1:5432/engram?sslmode=disable"

# A space + its founding space_admin principal + that principal's personal scope:
engraphy-admin space create --id demo --display-name "Demo space" --principal devon

# Give the space an ontology (the shipped starter pack, or your own — see doc 3):
engraphy-admin pack apply packs/starter/pack.yaml --space demo

# A display-once bearer token for a client/device:
engraphy-admin token create --space demo --principal devon --client-name laptop --role readwrite
```

The token prints **once** — copy it now; it is stored only as a SHA-256 and cannot
be retrieved again (`token revoke` + `token create` to replace a lost one).

> `space create` also mints the restore **sentinel** node and prints its id — that
> is expected; you never interact with it.

## 6. Run the server

The server reads the **app-role** URL and a bind address from the environment:

```bash
ENGRAPHY_DATABASE_URL="postgres://engram_app:devapppw@127.0.0.1:5432/engram?sslmode=disable" \
ENGRAPHY_BIND_HOST=127.0.0.1 \
ENGRAPHY_BIND_PORT=8000 \
  python -m engraphy.server.app
```

Boot order is: load config → **schema-version gate** (refuses to start if the DB is
behind the migrations shipped with the code — run `engraphy-admin migrate`) → warm
the embedding model → serve. It exposes three routes:

- `POST /mcp` — the MCP endpoint (Streamable HTTP), bearer-authenticated.
- `POST /inbox` — low-friction capture, same bearer auth.
- `GET /healthz` — unauthenticated liveness/version.

On a public interface the process **refuses to bind plaintext** unless
`ENGRAPHY_INSECURE_TRANSPORT_OK=true`; loopback and private ranges are exempt. See
[Deployment](05-deployment.md) for TLS.

## 7. Verify

```bash
curl -s http://127.0.0.1:8000/healthz
```

Expect something like:

```json
{"status":"ok","version":"0.1.0","schema_version":"0020",
 "spaces":1,"embedding_model":"nomic-ai/nomic-embed-text-v1.5"}
```

`schema_version` should match the highest-numbered file in
`engraphy/db/migrations/`. If the server refused to start with a
`SchemaVersionMismatch`, you skipped or partially ran step 4.

## 8. Connect a client & write your first memory

Point an MCP client (Claude Code, Claude Desktop, or any MCP-capable client) at
`http://127.0.0.1:8000/mcp` with an `Authorization: Bearer <token>` header. See
`deploy/clients.md` for per-client config. Then, over the tool surface, in order:

1. **`scope_list`** — proves the token resolves; on a fresh install shows exactly
   one writable scope, `personal-devon`.
2. **`write`** `{"scope":"personal-devon","type":"note","title":"Coffee machine",
   "body":"Descale the office coffee machine monthly.","attrs":{}}` → returns
   `outcome: "inserted"` and a node id.
3. **`search`** `{"scope":"personal-devon","query":"how to keep the espresso
   maker working"}` → recall is **semantic**, so a paraphrase finds it.
4. **`write`** the *same* note again (identical text) → comes back
   `outcome: "merged"` (deduplicated into the existing node — the envelope's
   `canonical.id` is the original) instead of creating a second one. Write a
   *re-worded* version of the same fact instead and you'll get
   `outcome: "merged_linked"` — kept as its own node, joined by a `same_topic`
   edge. Either way, a blind duplicate is never created — that's the dedup
   guarantee.

If step 2 fails with `ENGRAPHY_SCOPE_UNKNOWN`, the scope doesn't exist or isn't
writable — re-run `scope_list`, and create more with `scope_create`.

Next: [Build your own pack](03-packs.md) to define your own node/edge ontology, or
the [Tool reference](04-tools-reference.md) for every call in detail.
