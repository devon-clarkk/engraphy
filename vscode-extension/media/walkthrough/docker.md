# Run locally with Docker

The real, working setup today. All you need is **Docker** (with Compose). The
whole server runs on your machine; nothing is sent anywhere.

### 1. Get the repo

```
git clone https://github.com/devon-clarkk/engraphy.git
cd engraphy
```

### 2. Start it

The repo ships wrapper scripts that write a `.env` with random passwords, start
the stack, and then wait for the server to actually answer.

**macOS / Linux**

```
./up.sh
./provision.sh
```

**Windows (PowerShell)**

```
.\up.ps1
.\provision.ps1
```

`up` blocks until `/healthz` returns 200, then `provision` creates a space,
applies the starter pack, mints a token, and prints the URL and token to paste
in. Both are safe to re-run.

> First boot **downloads the embedding model (~523 MB)** into a volume before the
> server answers anything. That is roughly 45 seconds on a fast link and several
> minutes on a slow one, and it happens once. `up` waits it out for you.

### 3. Paste the URL and token

Run **Engraphy: Connect to a server (set URL + token)…** from the Command
Palette, or use the **I already have a server** step in this walkthrough.

- **Server URL**: `http://127.0.0.1:8000/mcp/` (keep the trailing slash)
- **Token**: the one `provision` just printed. It is shown **once** and stored
  only as a SHA-256 on the server, so copy it now. Lost it? Re-run `provision`
  for a fresh one.

The extension keeps the token in your OS keychain, not in `settings.json`.

That is it. The Engraphy icon in the Activity Bar should now show the
confirm-write queue instead of a "No server connected" screen.

---

### Prefer to run the commands yourself?

`up` and `provision` are thin wrappers. The same thing by hand:

```
printf 'POSTGRES_PASSWORD=%s\nENGRAPHY_APP_ROLE_PASSWORD=%s\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > .env

docker compose up -d

docker compose --profile admin run --rm admin \
  engraphy-admin space create --id personal --display-name "Personal" --principal me
docker compose --profile admin run --rm admin \
  engraphy-admin pack apply packs/starter/pack.yaml --space personal
docker compose --profile admin run --rm admin \
  engraphy-admin token create --space personal --principal me \
  --client-name vscode --role readwrite
```

`docker compose up -d` is the whole bring-up: an `init` sidecar runs the
migrations and provisions the database role after Postgres is healthy and before
the server starts. You do not run those separately, and the server will not start
until `init` exits cleanly.

**Engraphy: Start local server** in the Command Palette runs `docker compose up`
for you in a terminal, once `engraphy.composeWorkingDirectory` points at this
checkout.

### If something looks wrong

- `docker compose ps` shows the state. On first boot `engraphy` sits at
  `health: starting` while the model downloads. That is normal.
- `Restarting` is not normal. Read `docker compose logs engraphy`.
- Sanity check any time: `curl http://127.0.0.1:8000/healthz` should return
  `{"status":"ok",…}`. Note that `/healthz` needs no token, so a 200 there means
  the server is up, not that your token works. The status bar tells you that.

Full setup notes, including the no-Docker path:
<https://github.com/devon-clarkk/engraphy>
