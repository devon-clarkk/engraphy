# Engraphy — Operations and Governance

What makes this a product instead of a community reference implementation: deployment shape, backup contract, migration discipline for both engine and packs, versioning, and the operational answer to "the graph slowly accumulates errors nobody checks."

**Status:** Living document
**Last updated:** July 2026
**Scope:** Deployment, backups (contract, not implementation), engine migrations, pack migrations, release/versioning policy, hygiene mechanisms, client-capture guidance
**Out of scope:** Any specific deployment's host, network, and backup specifics (the operator owns those)

---

## Deployment shape

**The product is a single-instance service — one Python process + one Postgres per trust community — deployable locally or in the cloud.** *(Revised July 2026: the earlier "local-only" identity widened when teams arrived ([06](06-teams-and-sharing.md)) — a team needs a server all members can reach, and "a VM at a provider" is the honest default for a team without a Devon.)* What stays a non-goal, as product identity: **no managed multi-instance SaaS platform** — Engraphy is software you (or your team's operator) run, one instance per team/person, wherever. That line keeps everything simple that the local-only line used to: RLS instead of per-customer databases, an operator with shell access, instance-wide embedding model, `pg_dump` as the backup unit.

Two supported profiles, same artifact:

| | **Local / overlay** (self-hosted posture) | **Cloud** |
|---|---|---|
| Placement | Home server, tailnet/VPN, no public port | VM or container host (Fly.io / Hetzner / Railway-class), public endpoint |
| Transport | WireGuard is the encryption; TLS optional | **TLS mandatory** — the server *refuses* to serve auth over plaintext on a public interface unless `insecure_transport_ok: true` (documented as overlay-networks-only) |
| Install | systemd/launchd units (shipped) | **Docker image + compose file** (engraphy + postgres+pgvector) — shipped as a first-class artifact; the earlier no-Docker stance was a single-host hardware constraint, not product doctrine |
| Admin | Local CLI via SSH | Same — `engraphy-admin` over SSH to the VM/container; space-admins self-serve day-to-day via the MCP admin tools ([06](06-teams-and-sharing.md)) |
| Backups | Operator's scheduler | Same contract + provider volume snapshots as a bonus layer, never the primary (snapshots aren't `verify-restore`-tested dumps) |
| Hardening floor | Host firewall, overlay ACLs | TLS, rate limits (shipped), fail2ban-style ban list on repeated auth failures (shipped, in-process), tokens ≥ 256-bit (enforced), `/healthz` gated |

Ships with: unit files, Dockerfile + compose, `deploy/checklist.md` per profile (bind address, transport, token minting, first space + pack + principals), and the transport refusal above ([03](03-api-auth-and-tenancy.md)). The server image deliberately excludes `pg_dump`/`pg_restore`/`dbmate` (it ships what the server process needs); in the cloud profile the admin verbs that shell out to those binaries (`migrate`, `verify-restore`, ad-hoc backup dumps) run via the compose **admin sidecar** (same code + Postgres client tools, on the compose network), which is what preserves `migrate`'s unconditional pre-dump without publishing the database port or requiring a host toolchain. There is deliberately **no skip flag** on the pre-dump — "unconditional" is the safety property.

**Deployment configuration surface** (ratified July 2026 — E2 introduced these as an implementer choice, now contract): the server is configured by `ENGRAPHY_*` environment variables — `ENGRAPHY_DATABASE_URL` (required, no default), `ENGRAPHY_BIND_HOST` (default `127.0.0.1`), `ENGRAPHY_BIND_PORT` (default `8000`), `ENGRAPHY_INSECURE_TRANSPORT_OK` (literal `true` to opt in), `ENGRAPHY_LAST_BACKUP_STATUS_FILE` (path; unset = `/healthz` omits `last_backup_at`). Env vars, not a config file, because the codebase's own convention (`ENGRAPHY_TEST_DATABASE_URL`, `ENGRAPHY_BENCH_*`) already established the mechanism and both deployment profiles (systemd `EnvironmentFile`, compose `environment:`) consume env vars natively.

## Backup contract

Engraphy doesn't ship a backup system (a deployment brings its own external backup scheduler); it ships the **contract** any deployment must satisfy, and the hooks to keep it honest:

- Logical `pg_dump` is the supported backup unit; the DB is the only state (the process is stateless; packs and config live in git + the DB).
- `engraphy-admin verify-restore --against dump.pgdump` — the restore-test assertion suite (schema version match, per-space row counts, constraint tests against the restored DB, sentinel retrieval) packaged so *any* deployment's cron can run the monthly proof. Every Engraphy operator gets it, not just single-host deployments.
- **Sentinel convention** (decided July 2026): `engraphy-admin space create` mints one **sentinel node** per space — reserved engine-owned node type `engraphy_sentinel` (registered by `space create` itself, exempt from `doctor`'s registry-drift diff, and a reserved name `pack validate` refuses), fixed title/body, placed in the founding principal's personal scope, immediately `status='archived'` (so it can never surface in search, briefing, or dedup candidacy), with a **deterministic constant unit vector** as its embedding (model-free — `space create` must not need the embedding model loaded). Its id is stored in per-space config under `sentinel.node_id`; `verify-restore` reads that key per space and asserts the node's content survives the restore, falling back to `--sentinel-id` (operator-supplied) and then to skipped-with-log for pre-convention spaces. This makes the monthly proof self-contained instead of depending on an operator-maintained canary.
- **The client tools must match the server major version** (learned the hard way, 2026-07-21). `pg_restore` is not backward compatible in the direction that matters: a newer client emits prologue statements for GUCs the older server has never heard of (`SET transaction_timeout` arrived in 17), so restoring into an older server aborts on the first statement. The shipped sidecar therefore pins `postgresql-client-16` to match compose's `pgvector/pgvector:pg16`, and the pin moves with the server if the Postgres major is ever bumped. Any operator running `pg_dump`/`pg_restore` from a host rather than the sidecar owns the same constraint. The failure mode is nasty precisely because it is invisible until you need the backup: `migrate`'s unconditional pre-dump keeps succeeding (dumps are written by the client, not read by the server), so an instance can accumulate months of dumps it cannot restore. This is the argument for the drill being a *scheduled* ritual and not a one-off: only a restore proves a backup.
- `/healthz.last_backup_at` reads an operator-maintained status file: freshness is observable in-band by any client.
- Documented RPO guidance: dump cadence bounds loss; at personal write volumes 6h is the sweet spot.

## Engine migrations

As product policy: **dbmate**, plain SQL, manual application (`engraphy-admin migrate` wraps: unconditional pre-dump → `dbmate up` → restart → smoke test), boot-time version gate (server refuses to start on `schema_migrations` mismatch, naming both versions), additive-first, destructive changes across two releases, downs for development / restores for production. The embedding-model migration procedure (new column, resumable re-embed keyed on `embedding_model`, threshold recalibration against `dedup_log`, index rebuild) is the documented playbook.

## Pack migrations

New territory — packs evolve independently of the engine, per space:

- `engraphy-admin pack upgrade --space X pack.yaml` computes the registry diff and classifies each change:

| Change | Class | Behavior |
|--------|-------|----------|
| New type / new edge type / new rule / loosened attr-spec | additive | Applied immediately |
| New **required** attr, narrowed enum, `closed: false→true` | tightening | Applied **only after a conformance scan**: existing rows are checked against the new spec; violating rows are listed. Operator chooses: fix data first (the tool emits an `update` worklist), or apply with violators left in place as grandfathered rows |
| Removed type / removed rule | destructive | **Retire, never delete** (revised July 2026 — see below). Refused while rows/edges of that type exist with `status='active'`; once none do, the registry row is *retired*, not removed |

- Pack version recorded on the space; `pack.yaml` files are the *only* writers of registries — hand-edited registry rows are unsupported and flagged by `engraphy-admin doctor`.

**Nonconformance is derived, never stored** (reconciled July 2026 — the earlier `attrs_nonconforming` *flag* wording was never buildable without a second write path that could drift from the trigger). A nonconforming row can only be a grandfathered one from before a tightening upgrade (the validate trigger fires on `nodes`, not `node_types`, so tightening the registry never touches existing rows). `doctor` and `pack upgrade`'s conformance scan both derive the violation set on demand by asking `engraphy_validate_attrs(current_spec, attrs)` — the exact question the trigger answers on every write, via the same function, so report and enforcement cannot disagree. "Blocked from `update` until fixed" needs no mechanism at all: any `update` to a nonconforming row re-fires the trigger and fails with the named-key message unless the attrs are fixed in the same call. **Briefing-footer surfacing of nonconforming counts is cut** (decided July 2026): footers serve the agent mid-session, and an agent can neither see nor fix a pack spec — nonconformance is an *operator* state, created by an operator action (`pack upgrade`) and surfaced where the operator lives (`pack upgrade`'s scan output and `doctor`). If a pack someday wants agents repairing their own rows, that returns as a briefing *section*, not a footer.

**Destructive removal = retirement** (redesigned July 2026 — the original "archive-then-remove" wording assumed a final `DELETE FROM node_types`, which is unimplementable under [01](01-core-data-model.md)'s no-hard-deletes rule: historical rows of the type exist forever and hold the FK, whatever their status). "Remove" means **retire**: `node_types`/`edge_types` gain a `retired_at timestamptz` column (a small additive migration, to land with the first pack that actually retires a type — none shipped needs it yet). Semantics: a retired type **refuses new rows** (write-path check, trigger-backed, same two-layer posture as everything else) and is omitted from tool descriptions and pack listings; existing rows of the type remain readable, traversable, archivable — history is never orphaned, and every FK stays satisfied forever. `pack upgrade`'s destructive class becomes: refuse retire while `status='active'` rows/edges of the type exist (the operator archives content first, across two pack versions exactly as before); once none are active, set `retired_at`. A later pack version that re-declares the type simply clears `retired_at` — un-retiring is additive. `doctor`'s registry-drift diff treats a retired type as expected (present in the DB, absent from the pack file — that is the *correct* state, not drift).

## Versioning and release discipline

- **Semver on the engine.** Major = tool-signature or DDL-contract breaks; minor = new tools/constructs (attr-spec, briefing grammar additions are minor — old packs never break because grammars only grow); patch = fixes.
- A `COMPATIBILITY.md` table: engine version ↔ pack format version ↔ migration floor. **Pack-format check, as shipped and now the design** (reconciled July 2026): packs may declare `pack_format` (optional integer, default 1 — in the pack schema, [07](07-implementation-contracts.md)); `pack validate`/`apply`/`upgrade` **warn** (never refuse) when a pack declares a format newer than the engine's own `CURRENT_PACK_FORMAT` — the real interoperability hazard an operator hits (someone else's newer pack against an older engine). The earlier wording ("warns when a pack uses constructs newer than it declares") described a per-construct staleness check that would require tagging every schema field with the format version that introduced it; that scheme is **dropped as a v1 requirement** — it is not buildable without a versioning design nothing else needs yet, and the declared-format check covers the operational case. Revisit only if pack authorship ever leaves the trust circle.
- Release checklist: full test matrix (constraint suite via raw SQL, isolation fuzz, dedup bands, briefing grammar, both shipped packs from empty DB), migration up from the previous two releases, `verify-restore` against a previous-release dump.
- This is a one-maintainer product serving people who trust it with their memory; the discipline above is what "first-party quality" means at that scale — tests and gates instead of support staff.

## Hygiene: the "errors nobody checks" answer

The critique's sharpest operational point — an unreviewed graph rots — is answered by mechanisms, then by ritual:

| Mechanism (engine) | What it prevents |
|---|---|
| Dedup bands + forced-choice handshake | Duplicate accumulation, silent near-duplicate sprawl |
| Inbox staging | Auto-captured noise becoming memory without judgment |
| Resonance reports on every write | Contradictions/repeats invisible at write time |
| Provenance (`source_client`, `source_session`) + `purge-session` (semantics below) | Untraceable bad batches |
| Derived nonconformance reporting (`doctor`, `pack upgrade`'s scan — see [Pack migrations](#pack-migrations)) | Schema drift hiding quietly |
| `dedup_log` + `engraphy-admin doctor` (stale pendings, nonconforming counts, registry/pack drift, orphaned merges) | The instance's health being a mystery |
| Ritual (deployment) | Owner |
| Periodic consolidation pass (review stale/pending/nonconforming; scheduled via a session protocol on hook-capable clients, or a scheduled recap on hook-less ones) | The operator |

### `purge-session` — the poisoning-cleanup verb (semantics decided July 2026; build before E5's backfill)

`engraphy-admin purge-session --space X --session <source_session> [--dry-run]` — local CLI only, like every instance-admin verb. The threat it answers: a poisoned or bad session wrote a batch (a compromised client, a bad backfill run, a hallucinating recap job), and the operator needs everything it touched out of the agent-visible surface **without destroying the investigation record or violating no-hard-deletes**. Everything is archive/quarantine, never DELETE:

- **Nodes** authored with `source_session = S` and `status IN ('active','superseded')` → `status = 'archived'` — out of search, briefing, and dedup candidacy, still readable by id, reversible by hand. (Rows already `merged` keep their status: the merge-chain CHECK ties `merged` to `canonical_id`, reads already resolve them away to canonical, and their absorbed content is handled as addenda, next.)
- **Addenda** are the stealthiest vector — poison appended onto a *trusted* canonical node, served on every `get`. Each addendum object whose `source_session = S` gains an engine-managed `purged_at` timestamp: **quarantine, not jsonb surgery**. `get` excludes purged addenda from the wire; storage retains them (auditable, reversible, and the Jaccard novelty corpus still includes them — so re-telling the same poison is "not novel" and cannot be re-appended as fresh). This is why `source_session` is captured on merge addenda at all.
- **Parked pending writes** whose payload carries `source_session = S` → deleted (a parked write is a 24h scratch row, the one sanctioned DELETE class — migration 0011's precedent).
- **Not purgeable by session, stated honestly:** `edges` carry no provenance columns (v1) — an edge created by S between surviving nodes stays; the poison *payload* lives in nodes/addenda, and edges into archived nodes stop surfacing wherever archived nodes do. `inbox` captures carry no `source_session` — triage them via `inbox_review`/discard. `audit_log`/`dedup_log` are never purged: they are the investigation record.
- **Reporting:** `--dry-run` prints the full worklist and mutates nothing; a real run prints the same worklist plus one `audit_log` row for the invocation (action `purge_session`, detail: session id + per-category counts). Idempotent — a re-run matches nothing.

## Client capture guidance (the "manual process" critique)

Engraphy is pull/push, not ambient — by design (judgment stays with the agent). The capture story is therefore per-client-class, documented in `deploy/clients.md`:

- **Hook-capable harnesses** (Claude Code): auto-capture failures to `/inbox`, session-start briefing injection — the hook-driven session-protocol pattern, referenced as the exemplar.
- **Hook-less clients** (Claude Desktop/mobile — Alex's world): the tool descriptions carry the protocol ("call briefing first…", "when the user states a lasting preference, write it"); plus a **scheduled recap** pattern: a recurring prompt (vendor scheduled task or a custom cron runner) that reviews the day and writes distilled nodes — safe to run repeatedly *because dedup absorbs re-tellings*. That last clause is what makes low-diligence operation viable: the reference server punishes inconsistent habits with rot; Engraphy's write path is idempotent-ish by construction.
- Plan prerequisites (e.g., which Claude plans allow remote MCP connectors for a given surface) are a deployment-checklist item — verified per human before promising them memory.

## File reference

| File | Contents |
|------|----------|
| `deploy/checklist.md`, `deploy/clients.md`, `deploy/units/` | Operator docs + example units |
| `engraphy/admin/{migrate,verify_restore,doctor,packs}.py` | The operational verbs |
| `COMPATIBILITY.md`, `CHANGELOG.md` | Governance artifacts |

## Acceptance criteria

- [x] `engraphy-admin migrate`, `verify-restore`, `doctor`, and `pack upgrade` (all three change classes, including a forced conformance failure) demonstrated on a scratch instance. Evidence, all against a real pgvector Postgres in CI (the `test` job's service container) rather than mocks: `migrate` additionally runs for real on the deployment shape every push (`deploy-smoke`, sidecar, 0001→0017); `verify-restore`'s full sequence — real `pg_dump` → real `pg_restore` into a scratch DB → schema-version check → row counts → all three constraint probes → sentinel retrieval — executes in `test_shell_integration.py` (confirmed *executing*, not skipping, on run 29725971610 at 100% file coverage); `doctor` in `test_doctor.py`; `pack upgrade` in `test_pack_upgrade.py` across additive, tightening **with a forced conformance failure** (`test_tightening_with_violators_refused_without_flag`, plus the `--allow-nonconforming` grandfathering path) and destructive. **Two honest caveats:** (a) the destructive class as *implemented* still removes the registry row and lets the FK refuse the rest — the retire-not-delete redesign above (`retired_at`) is decided on paper and **not yet built**, so what is demonstrated is the pre-redesign behavior; (b) an end-to-end *operator* demonstration of `doctor`/`pack upgrade` on the compose profile has not been run (attempted 2026-07-21, blocked by a host-disk failure — see [05](05-roadmap.md)'s E3.5 row).
- [x] Boot version gate and pack-format warning demonstrated. Both are exercised as refusals/warnings rather than assumed: the gate in `test_app.py` (`check_schema_version` raising `SchemaVersionMismatch` on a doctored `schema_migrations`, and `expected_schema_version()` pinned to the latest migration), and it is the same gate every `deploy-smoke` boot passes through against a freshly migrated DB; the pack-format warning in `test_pack_format.py` (a pack declaring a future `pack_format` warns, a pack declaring the current one or omitting the field does not). The gate's *refusal* has not been staged on the compose profile — only in-process.
- [x] Release checklist executed once end-to-end for v0.1 (the discipline exists before the first user depends on it) — `RELEASE-CHECKLIST.md`'s v0.1.0 execution record: full test matrix (833 passed on CI), bench budgets, ruff + both guard scripts, `docker build` + healthy boot, COMPATIBILITY/CHANGELOG rows. *The checklist ran; no tag was cut* — a version tag waits for the first production release (ruling 2026-07-21), so item 7 of the checklist is deferred rather than executed.
- [ ] `deploy/clients.md` validated by really onboarding a hook-less client (Alex's Desktop) using only the doc. **Open** — this is [05](05-roadmap.md)'s E5 acceptance criterion and has not happened; the 2026-07-20 operator walkthrough was a dress rehearsal against `deploy/checklist.md`, not against `clients.md`, and not with Alex.
- [x] The cloud profile stood up once end-to-end (throwaway VM: compose up, TLS, space + two principals, client round-trip, teardown) — the profile is tested, not theoretical. Proven twice: the manual from-scratch Windows stand-up on 2026-07-20 (10/10 round trip), and unattended in CI on every push since — `deploy-smoke` (run 29763088585, commit 5b418f6) does compose up → sidecar migrate 0001→0017 → role provisioning → healthcheck green → space + pack + token → a 9/9 MCP round trip from a client with no `engraphy` code in-process → `down -v`. **TLS itself is out of scope of both runs by design**: compose deliberately ships no TLS terminator (the reverse proxy is the operator's, per §Deployment shape), so what is proven is the plaintext-loopback posture the compose file documents, not a live certificate.

**Deliberately not claimed:** the **local/overlay profile** has never been stood up. Its systemd/launchd units in `deploy/units/` are shipped and reviewed but unexecuted — neither is testable from the Windows dev box or from CI's containers — so "two supported profiles" is, as of E3 exit, one profile proven and one profile written down. First real overlay deployment is E4.
