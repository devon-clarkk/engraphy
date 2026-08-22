# Cloud profile throwaway-VM stand-up test (design/04 acceptance)

design/04's acceptance list: "The cloud profile stood up once end-to-end
(throwaway VM: compose up, TLS, space + two principals, client round-trip,
teardown) — the profile is tested, not theoretical."

## What's already proven, without a real VM

The mechanics that don't need real cloud infrastructure or a public
domain/TLS cert were verified during E3 development, directly against this
machine's Docker:

- `docker build .` succeeds (the `Dockerfile` produces a working image).
- The built image, run against a freshly-migrated + `deploy/provision-app-role.sql`-provisioned
  Postgres, boots cleanly: passes the boot-time schema version gate, loads
  the embedding model, and serves a healthy `GET /healthz`
  (`{"status":"ok","schema_version":"0016",...}`).
- `engraphy-admin` runs correctly inside the built image.
- `deploy/provision-app-role.sql` grants exactly the role/table/function set
  `engraphy/tests/conftest.py`'s `_ensure_app_role` fixture asserts is correct
  (37 table grants + 3 function grants, verified by direct query against a
  scratch DB), and is idempotent (safe to re-run).

This covers everything about the cloud profile that's testable without an
actual public IP, a real domain, and a TLS certificate — which is most of
the risk surface (the Dockerfile, the compose wiring, the role provisioning,
the boot sequence). What it does NOT cover: `compose.yaml`'s multi-service
orchestration exactly as written (only the `engraphy` image was tested
standalone, not `docker compose up` against a `postgres` service defined the
same way), a real reverse-proxy TLS setup, and the actual "reachable from
the internet, not just from this host's Docker network" property.

## What still needs a real VM (not run here)

Standing up an actual cloud VM means: choosing a provider, provisioning
compute (which costs money and creates a resource outside this machine that
needs cleanup), and pointing a domain at it for TLS. That's a real-world
provisioning action with cost and blast-radius outside this repo — not
something to do autonomously without the operator's explicit go-ahead on
which provider/account/budget to use. The runbook below is what to run once
that's decided; nothing here was executed against a real VM as part of this
phase.

### Runbook

1. **Provision a throwaway VM.** Any provider works (design/04 names
   Fly.io/Hetzner/Railway-class as the reference tier) — smallest instance
   size that can hold the embedding model in memory (a few GB RAM is
   sufficient; no GPU needed). Install Docker + the Docker Compose plugin.
2. **Point a domain (or subdomain) at it**, even a throwaway one (a free DNS
   provider or the cloud provider's own `*.nip.io`-style wildcard works for
   a test) — TLS via Caddy/Let's Encrypt needs a real hostname to issue a
   cert against.
3. **Copy the repo (or clone the release tag) onto the VM.**
4. Follow `deploy/checklist.md`'s "Cloud profile" section verbatim, steps
   1-8 (`.env`, Postgres up, migrate, provision-app-role, TLS reverse proxy,
   `docker compose up -d`, first space/pack/principals, firewall).
5. **Two principals + client round-trip** (the part step 7 gestures at but
   this test makes explicit):
   ```
   engraphy-admin principal add --space <space> --id dev-a --display-name "A"
   engraphy-admin principal add --space <space> --id dev-b --display-name "B"
   engraphy-admin token create --space <space> --principal dev-a --client-name test-a --role readwrite
   engraphy-admin token create --space <space> --principal dev-b --client-name test-b --role readwrite
   ```
   From two separate machines (or two terminals with two different bearer
   tokens), connect an MCP client (or `curl` a raw `tools/call` against
   `/mcp`) through the public HTTPS endpoint and confirm each principal's
   `write` + `get` round-trips, and that the isolation model holds (dev-a's
   private-scope write is not visible to dev-b — the cross-space/cross-
   principal fuzz suite's live-instance analog, at small scale).
6. **Teardown.** `docker compose down -v` (drops the named volumes too —
   this is a throwaway instance, not one to keep), then destroy the VM and
   release the domain/DNS record.

### Recording the result

Once run, append a dated note here (or in `DECISIONS-DELTA.md`, matching
house style) with: provider used, whether each runbook step succeeded as
written, and any doc fixes the run surfaced — the same "gaps found become
doc fixes" principle E5's client-onboarding acceptance criterion uses.
