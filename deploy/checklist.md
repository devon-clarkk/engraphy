# Deployment checklist

Two supported profiles (design/04 s.Deployment shape), same artifact (this
codebase); different install/transport posture. Pick one section below and
follow it in order — each step depends on the one before it.

Both profiles need, before you start:

- A Postgres 16 server with the `pgvector` extension installed (`pgvector/pgvector:pg16`
  covers this if you're running Postgres in a container even for the local
  profile; a native install needs `CREATE EXTENSION vector` to succeed, which
  means the extension's shared library must already be on the server).
- **Local/overlay profile only:** `dbmate` and the Postgres client tools
  (`psql`, `pg_dump`, `pg_restore`) on the machine that will run
  `engraphy-admin` — `engraphy-admin migrate` and `verify-restore` shell out to
  them. The **cloud profile needs none of these on the host**: they ship in
  the `admin` sidecar container (see that section).
- This repo (or the release tag you're deploying) checked out, with
  `pip install .` (or `.[dev]` if you'll also run tests) into a venv.

### About the `engraphy-admin` command

`pip install .` declares an `engraphy-admin` console script. It lands in your
venv's `bin/` (POSIX) or `Scripts\` (Windows) directory, so it is only on your
PATH while **that venv is activated** — a very common first-run stumble is
running `engraphy-admin` from a shell where the venv isn't active and getting
`command not found`.

Two equally-valid ways to invoke it, used interchangeably below:

```
engraphy-admin <verb> ...              # venv activated
python -m engraphy.admin.cli <verb> ...  # always works from the repo root, no PATH needed
```

If `engraphy-admin` isn't found, activate the venv (`source .venv/bin/activate`,
or `.venv\Scripts\activate` on Windows) or use the `python -m` form. In the
cloud profile you mostly won't need either on the host — the sidecar runs
`engraphy-admin` inside the container.

> **Connection URLs: prefer `127.0.0.1` over `localhost`.** On Windows (and
> anywhere `localhost` resolves to `::1` first) a published container port is
> bound to `127.0.0.1` only, so `localhost` costs a ~10s IPv6 connection
> timeout *per connection* before falling back to IPv4 — every admin command
> pays it. The URLs below use `127.0.0.1` deliberately.

> **Windows + Git Bash / MSYS: prefix container commands with
> `MSYS_NO_PATHCONV=1`.** Git Bash rewrites any argument that looks like a
> Unix absolute path into a Windows path *before* the container sees it, so
> `--dump-dir /backups` arrives inside the Linux container as `C:/Program
> Files/Git/backups` and fails with a baffling
> `PermissionError: [Errno 13] Permission denied: 'C:'`. This affects every
> `docker compose run` below that passes an absolute in-container path:
> ```
> MSYS_NO_PATHCONV=1 docker compose --profile admin run --rm admin ...
> ```
> PowerShell, cmd, WSL, macOS and Linux shells are unaffected. (Doubling the
> leading slash — `//backups` — also works but is easier to get wrong.)

---

## Local / overlay profile (single-user posture)

Placement: home server, reachable only over a tailnet/VPN, no public port.
Transport: the overlay network (WireGuard/Tailscale) IS the encryption — TLS
is optional here, matching design/04's table. Install: systemd (Linux) or
launchd (macOS) unit, not Docker — see `deploy/units/`.

1. **Bind address.** Pick an address on the overlay only — never `0.0.0.0` or
   a bare public interface. A tailnet IP (`100.x.y.z`) or `127.0.0.1` if a
   separate reverse-proxy/tailscale-serve process fronts it locally. Set
   `ENGRAPHY_BIND_HOST` to that address, `ENGRAPHY_BIND_PORT` (default matches
   `8000` in the unit files, change both together if you pick a different
   port).
2. **Transport.** Nothing to configure in engraphy itself — `ENGRAPHY_INSECURE_TRANSPORT_OK`
   stays unset/false; `check_transport_security` (`engraphy/server/app.py`)
   passes automatically for loopback/RFC1918/CGNAT/tailnet-range addresses
   without needing that opt-in. If you ever bind a genuinely public address
   even on this profile, either put TLS in front or set
   `ENGRAPHY_INSECURE_TRANSPORT_OK=true` **only** if the interface is truly
   overlay-only (design/04: "overlay-networks-only" is the one sanctioned use).
3. **Provision the DB role.** Once, against the target database, after the
   schema exists (next step creates it):
   ```
   psql "$ENGRAPHY_DATABASE_URL_SUPERUSER" \
     -v app_role_password="$(openssl rand -base64 32 | tr -d '\n')" \
     -f deploy/provision-app-role.sql
   ```
   Save the generated password — it goes into `ENGRAPHY_DATABASE_URL` below.
4. **Run the schema up.** `engraphy-admin migrate` wraps the full sequence
   (unconditional pre-dump, `dbmate up`, restart, smoke test) — on a
   brand-new DB the pre-dump is nearly empty and the restart step has
   nothing to restart yet, which is fine:
   ```
   ENGRAPHY_DATABASE_URL=postgres://postgres:...@127.0.0.1/engram \
     engraphy-admin migrate --dump-dir /var/backups/engraphy
   ```
   (Superuser/owner connection for this step — the app role from step 3
   deliberately cannot run migrations.)
5. **Install and start the unit.** Copy `deploy/units/engraphy.service`
   (Linux) or `deploy/units/com.engraphy.server.plist` (macOS), fill in
   `ENGRAPHY_DATABASE_URL` with the app-role credentials from step 3, then:
   - Linux: `sudo cp ... /etc/systemd/system/`, create `/etc/engraphy/engraphy.env`
     (mode 0600) with the env vars, `systemctl enable --now engraphy`.
   - macOS: `launchctl load ~/Library/LaunchAgents/com.engraphy.server.plist`.
6. **First space + pack + principals**, using `engraphy-admin` against the
   *superuser* connection (space/token administration is local-CLI-only by
   design — design/03: "no code path" over the network):
   ```
   engraphy-admin space create --id <space> --display-name "..." --principal <you>
   engraphy-admin pack apply packs/starter/pack.yaml --space <space>
   engraphy-admin token create --space <space> --principal <you> --client-name <device> --role readwrite
   ```
   The token prints once — store it in your client's MCP config now.
7. **Healthcheck.** `curl http://<bind-host>:<port>/healthz` — expect
   `{"status": "ok", "schema_version": "...", ...}`. `schema_version` should
   match the highest-numbered file in `engraphy/db/migrations/`.
8. **Backup wiring.** Point your scheduler (cron, launchd calendar job,
   or any external backup scheduler) at a `pg_dump --format=custom` of the DB on your RPO
   cadence (design/04: 6h is the documented personal-scale sweet spot), then
   have it write the completion timestamp to whatever path you set
   `ENGRAPHY_LAST_BACKUP_STATUS_FILE` to, e.g.:
   ```
   pg_dump --format=custom --file "$BACKUP_DIR/$(date +%Y%m%dT%H%M%S).pgdump" "$ENGRAPHY_DATABASE_URL_SUPERUSER" \
     && date -u +%Y-%m-%dT%H:%M:%SZ > /var/lib/engraphy/last_backup_at
   ```
   Run `engraphy-admin verify-restore --against <dump>` monthly against one of
   these dumps — this is the "restore-tested, not just taken" proof.

## Cloud profile

Placement: a VM or container host (Fly.io / Hetzner / Railway-class), public
endpoint. Transport: **TLS mandatory** — the server refuses to serve auth
over plaintext on a public interface unless `insecure_transport_ok: true`
(read the transport-security note at the bottom of this file before relying
on that refusal as your only safeguard). Install: Docker image + `compose.yaml`.

1. **`.env` file.** Next to `compose.yaml`, create a git-ignored `.env` with:
   ```
   POSTGRES_PASSWORD=<random>
   ENGRAPHY_APP_ROLE_PASSWORD=<random>
   ```
2. **Bring up Postgres first.** `docker compose up -d postgres`, wait for it
   healthy (`docker compose ps` shows `(healthy)`).
3. **Run migrations, via the `admin` sidecar.** The sidecar image carries
   `dbmate` + `psql`/`pg_dump`/`pg_restore`, so **nothing needs installing on
   the host** and postgres needs no published port — the sidecar is on the
   compose network. It is a one-shot container (`--rm`), not a running service:
   ```
   docker compose --profile admin run --rm admin \
     engraphy-admin migrate --migrations-dir engraphy/db/migrations --dump-dir /backups
   ```
   `--migrations-dir` is passed explicitly above, but is **no longer
   required**: the migration `.sql` files now ship as package data
   (`pyproject.toml`), so the default — derived from the installed package's
   own location — resolves correctly even for a non-editable `pip install .`
   outside the repo. Pass it when you deliberately want a *different*
   migrations directory (e.g. a vendored copy); otherwise `engraphy-admin
   migrate --dump-dir /backups` is enough.

   `ENGRAPHY_DATABASE_URL` is already set in the sidecar's environment to the
   **superuser** connection (`compose.yaml`), which is what migrations need.
   The pre-migrate dump lands in the `backups` **named volume**, not in a
   host directory — a bind mount would come up root-owned (or owned by the
   wrong uid) on a fresh checkout and the sidecar, which runs as uid 1000,
   could not write into it. Copy dumps out with:
   ```
   docker compose --profile admin cp admin:/backups ./backups
   ```
   **`docker compose down -v` destroys that volume** along with the database,
   so copy anything you want to keep out before tearing down. On a brand-new
   DB the pre-dump is nearly empty and there is nothing to restart yet — fine.
   (`migrate` takes that dump unconditionally on purpose: it is the safety
   property, which is why the sidecar exists instead of a skip flag.)
4. **Provision the app role**, same script, also via the sidecar:
   ```
   docker compose --profile admin run --rm admin \
     psql "$ENGRAPHY_DATABASE_URL" \
       -v app_role_password="$ENGRAPHY_APP_ROLE_PASSWORD" \
       -f deploy/provision-app-role.sql
   ```
   (Both variables are already in the sidecar's environment. The script prints
   `CREATE ROLE` / `GRANT` tags but deliberately never echoes the password.)
5. **TLS.** Put a reverse proxy (Caddy, nginx, Traefik) or your provider's
   load balancer in front, terminating TLS on 443 and forwarding to
   `127.0.0.1:8000` (`compose.yaml`'s default `ports:` mapping — engraphy's own
   port is never exposed directly). Caddy's automatic-HTTPS is the lowest-effort
   option for a bare VM:
   ```
   your-domain.example { reverse_proxy 127.0.0.1:8000 }
   ```
6. **Bring up engraphy.** `docker compose up -d engraphy` (or `docker compose up -d`
   for both services at once, now that migrations are applied).

   **First boot downloads the embedding model (~523MB) into the `model-cache`
   volume before it serves anything.** On a fast connection that is roughly
   **45 seconds**; on a slow one it can be many minutes. Watch readiness with
   the compose healthcheck rather than guessing:
   ```
   docker compose ps          # engraphy: "(health: starting)" -> "(healthy)"
   docker compose logs -f engraphy
   ```
   The healthcheck's `start_period` covers a slow download, so a long download
   shows as **starting** while a genuinely broken boot eventually shows as
   **unhealthy**. That distinction matters: the failure mode to watch for is a
   **crash-loop being mistaken for a slow download** (`docker compose ps`
   showing `Restarting`, `/healthz` never answering). If you see `Restarting`,
   stop waiting and read `docker compose logs engraphy` — it is a real error,
   not the model.
7. **First space + pack + principals + healthcheck + backup wiring** —
   identical to the local profile's steps 6-8 above. Which container to run
   `engraphy-admin` in:
   - **Plain DB verbs** — `space` / `principal` / `token` / `config` /
     `import` / `pack validate|apply|upgrade` / `doctor` — run in either the
     `admin` sidecar or the `engraphy` container; the sidecar is the consistent
     choice:
     ```
     docker compose --profile admin run --rm admin \
       engraphy-admin space create --id <space> --display-name "..." --principal <you>
     ```
   - **`migrate` / `verify-restore` / ad-hoc `pg_dump`** — **must** use the
     `admin` sidecar. The `engraphy` server image deliberately ships no
     `pg_dump`/`pg_restore`/`dbmate` (see the Dockerfile header), so
     `docker compose exec engraphy engraphy-admin migrate` fails with
     "pg_dump not found on PATH". The sidecar exists precisely for these.
   - Running them from the **host** also works, but only if you have those
     tools installed *and* publish postgres's port (commented out in
     `compose.yaml` by default). The sidecar avoids both requirements.
8. **Firewall/security-group.** Confirm only 443 (your reverse proxy) is
   open to the internet; engraphy's own port and postgres's port must not be.

---

## Your first memory (both profiles)

The checklist above leaves you with a running server, a space, a pack, and a
token — but not an obvious answer to "what do I actually write to?". Scopes are
not created by the pack; the only one that exists after
`space create --principal <you>` is that principal's personal scope:

```
personal-<principal>        # e.g. personal-devon -- private, ambient, owned by you
```

Never guess it — ask the server, which is also your first end-to-end proof that
the token works. From a connected MCP client (see `deploy/clients.md`), or over
plain HTTP:

```
curl -s http://127.0.0.1:8000/healthz          # no auth; proves the server is up
```

Then, through an MCP client with your bearer token, call in this order:

1. `scope_list` → confirms the token resolves and shows exactly which scopes
   you may write to (expect `personal-<principal>` on a fresh install).
2. `write` → `{"scope": "personal-<you>", "type": "note", "title": "...",
   "body": "...", "attrs": {}}`. Returns `outcome: "inserted"` and the new
   node's id. (`note` is a starter-pack type; `pack apply` created the type but
   no scopes.)
3. `search` → `{"scope": "personal-<you>", "query": "<a paraphrase of it>"}`.
   Recall is semantic, so a paraphrase should find it — that is the actual
   product working, not just persistence.
4. `briefing` → `{"scope": "personal-<you>"}` shows it under `recent_notes`.
   Pass a `hint` too (`{"scope": ..., "hint": "..."}`) if you want the
   semantic `relevant` section populated — without a hint that section is
   empty by design, which is not a fault.
5. Optionally write a *near-duplicate* of step 2. It should come back
   `outcome: "merged"` with a high `similarity` rather than creating a second
   node — that is the dedup guarantee the whole design rests on.

If step 2 fails with `ENGRAPHY_SCOPE_UNKNOWN`, you used a scope that doesn't
exist or isn't writable — re-run `scope_list`. Create more scopes with
`scope_create` (or `engraphy-admin`) as you need them.

---

## Transport-security note (read before relying on it)

There is a wording conflict between design/03 (s.Transport: "Engraphy logs a
prominent warning at startup ... without TLS" — implying it still boots) and
design/04 (s.Deployment shape: "the server *refuses* to serve auth over
plaintext on a public interface unless `insecure_transport_ok: true`"). The
shipped code (`engraphy/server/app.py::check_transport_security`, exercised by
`engraphy/tests/test_app.py`) implements design/04's behavior: it raises and
the process exits nonzero, it does not merely warn. This has NOT been
reconciled between the two design docs — flagged in `DECISIONS-DELTA.md`
("transport-security wording conflict, 03 vs 04") for Devon to resolve which
doc's wording was stale. Operationally: treat the refusal as real (because
it is, in the code you're running), but don't be surprised if design/03's
prose still describes the old behavior until that's folded back.

Separately: this refusal is a *bind-host classification* check
(loopback/RFC1918/CGNAT = exempt, everything else needs the opt-in), not a
guarantee about what's actually reachable from the internet. A container
bound to `0.0.0.0` is classified "private" by the check (Python's
`ipaddress` module treats `0.0.0.0` that way) and will boot without the
opt-in even though `0.0.0.0` inside a container is exactly what makes it
reachable via a published port. The real boundary for the cloud profile is
`compose.yaml`'s `ports:` mapping (bound to loopback, forcing a reverse
proxy) plus your firewall/security-group — not this check. Don't treat a
clean boot as proof the deployment is actually private.
