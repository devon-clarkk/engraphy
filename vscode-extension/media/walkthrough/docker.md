# Run locally with Docker

The real, working local setup today. You need **Docker** (with Compose) and a
checkout of the Engraphy repo (`compose.yaml` lives at its root).

> **Future one-liner (not published yet).** A single-command image
> `docker run ghcr.io/devon-clarkk/engraphy:<tag>` is planned but **not published
> today** — don't rely on it yet. Until then, use the compose steps below.

### 1. Create a `.env` next to `compose.yaml`

```
POSTGRES_PASSWORD=<a random secret>
ENGRAPHY_APP_ROLE_PASSWORD=<another random secret>
```

### 2. Bring up Postgres and run migrations (admin sidecar)

```
docker compose up -d postgres
docker compose --profile admin run --rm admin \
  engraphy-admin migrate --dump-dir /backups
docker compose --profile admin run --rm admin \
  psql "$ENGRAPHY_DATABASE_URL" -v app_role_password="$ENGRAPHY_APP_ROLE_PASSWORD" \
  -f deploy/provision-app-role.sql
```

### 3. Start the server

```
docker compose up -d engraphy
```

> First boot **downloads the embedding model (~523 MB)** into a volume before it
> serves anything (~45s on a fast link, longer on a slow one). Watch readiness:
> `docker compose ps` (engraphy goes `health: starting` → `healthy`). If it shows
> `Restarting`, read `docker compose logs engraphy` — that's a real error, not the
> model download. You can also use **Engraphy: Start local server** from the
> Command Palette to run `docker compose up` for you.

### 4. Create a space, apply a pack, and mint a token

```
docker compose --profile admin run --rm admin \
  engraphy-admin space create --id personal --display-name "Personal" --principal me
docker compose --profile admin run --rm admin \
  engraphy-admin pack apply packs/starter/pack.yaml --space personal
docker compose --profile admin run --rm admin \
  engraphy-admin token create --space personal --principal me \
  --client-name vscode --role readwrite
```

The token prints **once** — copy it now.

### 5. Point the extension at it

Set the URL to `http://127.0.0.1:8000/mcp/` (keep the trailing slash) and paste
the token: use **"I already have a server"** in this walkthrough, or run
**Engraphy: Connect to a server (set URL + token)…**.

Sanity check any time: `curl http://127.0.0.1:8000/healthz` → `{"status":"ok",…}`.
