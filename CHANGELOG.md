# Changelog

## Unreleased
- Engram -> Engraphy rename completed inside the database (migration 0024). The
  identifiers COMPATIBILITY.md previously froze are all renamed: the
  `engram_*()` SQL functions to `engraphy_*()`, the `engram.space_id` /
  `engram.principal` session GUCs to `engraphy.*`, the `engram_app` role to
  `engraphy_app`, and the reserved node type `engram_sentinel` to
  `engraphy_sentinel`. New installs get a database named `engraphy`. There are
  no `engram_` table prefixes and no `engram` SQL schema, so nothing was needed
  there. Upgrading an existing deployment is a one-time operator sequence
  (stop server, migrate, re-run `deploy/provision-app-role.sql`, update DSNs to
  the new role name); see COMPATIBILITY.md. Entries below this one predate the
  rename and name the identifiers as they were at the time.
- Design phase complete (design/ 00–07 + implementation plans). Scaffold committed.
- E0 schema & enforcement kernel (migrations 0001–0011): pgvector schema, attr-spec interpreter,
  visibility functions + RLS on every data table, pack validate/apply, golden fixtures + CI.
- E1 engine behaviors: embedding pipeline (nomic-embed-text-v1.5, task prefixes); Jaccard + banded
  dedup write path with resonance, advisory-lock race guard, supersede, import-mode gates (migrations
  0010–0011); hybrid search (RRF) + recall stats (migration 0012); briefing engine — declarative
  section interpreter, shared hybrid_fuse with search, semantic-section relevance floor, inbox_read
  RLS policy (migration 0013); recursive-CTE traverse; inbox capture/list/promote/discard with write
  policies (migration 0014); bulk import (engraphy/admin/import_.py) — JSONL batch through the write
  pipeline in import mode, review-queue CSV, idempotent by construction (test_import.py, incl. the
  1,000-item synthetic-backfill acceptance case). Remaining before E1 exit: bench.py perf budgets +
  acceptance list green in CI.
- E2 server, auth, admin: FastMCP-based server (`engraphy/server/app.py`) over Streamable HTTP —
  `/mcp`, `/inbox`, `/healthz`; bearer tokens (space/principal/client/role), rate limits, ban-on-
  repeated-auth-failure; tool aliases + per-space description assembly; audit log; boot-time schema
  version gate (refuses to start on a `schema_migrations` mismatch); transport-security refusal
  (plaintext auth on a public interface exits nonzero without explicit opt-in); `engraphy-admin`
  `space`/`principal`/`token`/`config`/`import`/`pack validate`/`pack apply` verbs (migration 0016,
  the RLS write policies the four `admin_*` MCP tools need). bench.py's search p50 budget
  recalibrated 120 -> 175ms against observed GitHub-runner variance (evidence-based,
  measured on CI).
- E3 productization: `engraphy-admin migrate` (unconditional pre-dump -> `dbmate up` -> restart ->
  smoke test), `verify-restore` (restore-test assertion suite against a scratch DB — schema version,
  per-space row counts, constraint probes, sentinel retrieval), `doctor` (stale pendings,
  attrs_nonconforming — derived, not stored, registry-vs-pack drift, orphaned merges, canonical
  chains >3, nodes with >20 addenda), `pack upgrade` (additive/tightening/destructive change
  classes, diffed against the space's current registry). Deploy docs for both profiles: `Dockerfile`
  + `compose.yaml` (cloud), systemd/launchd units (local/overlay), `deploy/provision-app-role.sql`
  (closes the app-role `schema_migrations` GRANT gap CI caught in E2), `deploy/checklist.md`,
  `deploy/clients.md`. Pack-format warning (`pack_format` field, `pack apply`/`pack upgrade` warn on
  a pack declaring a newer format than this engine understands). `COMPATIBILITY.md` engine/pack-
  format/migration-floor table.
- Kernel fix (migration 0017): `attrs.addenda` is now exempt from the attr-spec closed-spec
  unknown-key check, on BOTH interpreters (`engram_validate_attrs()` in plpgsql and
  `engraphy.core.attr_spec`'s Python mirror, held identical by the parity fuzzer). Without it the
  dedup merge write — an UPDATE that re-fires the validate trigger — raised `CheckViolation` on any
  node type declared `closed: true`, which is every node type in both shipped packs: no node could
  receive a second dedup occurrence on a stock deployment. `RESERVED_ATTR_KEYS` is now the single
  source of truth for the reserved-key rule (`dedup.py`/`update.py` previously each hardcoded the
  literal). `test_attr_spec_parity.py`'s generated-key pool gained `addenda` — without it the two
  interpreters could only ever agree by both being wrong, which is how this shipped.
- Deploy packaging, from the 2026-07-20 operator walkthrough (twelve findings, all engine-external —
  the engine itself was never implicated): model-cache directory created and chowned in the image
  before `USER engraphy`, ending the root-owned-volume crash-loop; a real `/healthz` **healthcheck** on
  the `engraphy` compose service, which is what had made that crash-loop indistinguishable from "Up";
  `WindowsSelectorEventLoopPolicy` set at `engraphy-admin` startup, without which every async admin verb
  (`token create`, `import`) was dead on Windows — i.e. no token, therefore no client, on the dev
  machine's own platform; an `admin` **sidecar** service carrying `pg_dump`/`pg_restore`/`psql`/`dbmate`
  so `migrate` keeps its unconditional pre-dump without a host toolchain or a published database port
  (no skip flag was added — "unconditional" is the safety property); `/backups` as a named volume
  seeded engraphy-owned from the image; migrations and the pack schema shipped as package data;
  `127.0.0.1` instead of `localhost` throughout the checklist (Windows IPv6-first stalls); the
  app-role provisioning script no longer echoing the password; and an actionable error when the
  embedding-model cache is unwritable.
- CI now tests the product, not just the code: a **deploy-smoke** job that follows `deploy/checklist.md`'s
  cloud profile end to end on every push (compose up → sidecar migrate → role provisioning →
  healthcheck → space + pack + token → a 9/9 MCP round trip from a client with no `engraphy` code
  in-process → `down -v`), and a **windows-cli** lane guarding the admin CLI's event-loop policy.
  Run 29763088585 (commit 5b418f6) is the first fully green run of all three jobs.
- Design fold-back: `design/01`–`08` now describe the built system (~40 design-decision entries and a
  dozen resolved open questions folded in), including the transport refusal, derived-not-stored attrs
  nonconformance with the briefing footer cut, retire-not-delete for destructive pack upgrades, the
  `ENGRAPHY_*` config surface as contract, and the `verify-restore` sentinel convention. `design/05`
  gained an explicit **E3.5** gate between E3 and E4's one-way door.
- **E3 acceptance closed out** (2026-07-21): the operational verbs, the boot version gate and
  pack-format warning, the release-checklist run, and the cloud profile stood up end to end (twice —
  manually on Windows and unattended in CI). Left open, deliberately: the **local/overlay profile**
  has never been stood up (its systemd/launchd units are shipped but unexecuted, and are not testable
  from Windows or from CI containers — first real run is the first deployment); `deploy/clients.md` validation is
  E5's criterion and has not happened; the `verify-restore` **sentinel** is designed but unbuilt
  (`space create` mints no `engram_sentinel`; only the operator-supplied `--sentinel-id` path exists);
  the pack-upgrade **retire** semantics (`retired_at`) are decided but unbuilt; and the E3.5
  backup/restore drill is still outstanding — attempted 2026-07-21 and blocked by a host-disk failure
  on the dev machine, not by anything in this repo. No version tag has been cut: tagging waits for the
  first production release.
- `phase/e3` merged to `main` (2026-07-21, fast-forward, 122 commits). Still untagged, by standing
  instruction.
- **verify-restore sentinel implemented** (design/04 §Backup contract): `space create` registers the
  reserved `engram_sentinel` type and mints one archived node per space carrying a deterministic
  constant unit vector — no embedding model needed by the bootstrap verb — with its id in per-space
  config under `sentinel.node_id`; `verify-restore` resolves it per space (config → `--sentinel-id` →
  skip-with-log) and compares content, not just presence. `pack validate` now refuses both names
  design/07 reserves (`engram_sentinel`, and the `addenda` attrs key, which had never been checked).
  Two consequences the design had not anticipated: search and briefing exclude the sentinel **by
  type**, because archived status alone is defeated by search's agent-callable `include_inactive` and
  by a pack briefing section declaring `status: archived`; and `pack upgrade` is exempted from
  treating it as a destructive removal, without which every upgrade of every space would be refused.
  **Verified 2026-07-21**: `test_sentinel.py` 15/15 against a real Postgres, full suite 846 passed /
  4 skipped (migrations from empty), and proven end-to-end by the backup/restore drill below. Bench
  confirms the new search-path predicate costs nothing measurable (search p50 115.1 ms vs a 175 ms
  budget). Two defects were caught by static review before the first run — a float4 tolerance that
  would have failed against pgvector's single-precision storage, and a shared-DB assertion.
- **E3.5's backup/restore drill executed** (2026-07-21) on the cloud profile: `pg_dump` from the
  populated compose instance → `verify-restore` into a fresh scratch database (schema version, row
  counts, three constraint probes, sentinel located from config and content-matched, pre-convention
  spaces skipped-with-log) → plus an independent restore into a clean database, inspected rather than
  dropped, confirming the 0.99-merged node returned with an md5-identical addendum and all 13 tables
  matching row-for-row.
- **Fix found by that drill:** the admin sidecar installed an unpinned `postgresql-client` (17 on
  trixie) against a pg16 server, so `pg_restore` emitted a `SET transaction_timeout` that pg16
  rejects — `verify-restore` was broken on the shipped cloud profile. Now pinned to
  `postgresql-client-16` with a version assertion in the same layer. The reason it shipped is the
  larger fix: `deploy-smoke` ran `migrate`, whose pre-dump only ever *writes* a dump, so nothing in
  CI had ever read one back. deploy-smoke now takes a dump and runs `verify-restore` against it,
  asserting the sentinel resolved from config rather than trusting the exit code.
- **E3.5 closed** by CI run `29803411868` on `e759198`: `test`, `deploy-smoke` and `windows-cli` all
  green, verified from the job log rather than the checkmark. The admin image built
  `postgresql-client-16` (16.14-1.pgdg13+1) from PGDG, the in-layer `pg_restore --version` assertion
  held, and the new drill step restored a real dump into a scratch database, logging
  `sentinel … retrieved from config (status 'archived', content matches)` for a sentinel minted by
  CI's own `space create` on a freshly built image. **This proves the restore path on the
  cloud/compose profile only** — the local/overlay profile remains unexecuted and unclaimed, with its
  systemd/launchd units shipped but never run (first real overlay deployment is still ahead).
- History note: the float4 tolerance defect in `test_sentinel.py` was caught by static review, but
  only after it had already failed CI twice (runs `29793430641`, `29793640374`), where it presented
  as a bare `assert False` and read like a broken mint rather than a wrong assertion. Fixed in
  `cdb5e25`.
- Engine version is now single-sourced from `engraphy/__init__.py`'s `__version__` (pyproject resolves
  it via setuptools `attr:`; `/healthz` reads the same attribute), replacing three hand-maintained
  copies that nothing kept in agreement.
- Per-space usage metrics + the read-only `stats` MCP tool (migration 0021, `engraphy/core/metrics.py`).
  A pre-aggregated day-bucketed rollup table `metrics_rollup` (space_id, principal, metric,
  bucket_date -> bigint count), upserted `ON CONFLICT DO UPDATE`, RLS-scoped to the space (SELECT) and
  principal (INSERT/UPDATE). Six metrics counted at their core chokepoints — questions_asked /
  answered / memory_reused (search), facts_stored / duplicates_prevented (write + resolve_duplicate),
  promotes (inbox promote) — each definition frozen in `core/metrics.py`'s docstring for the extension
  to mirror. `stats(range_days?=30, group_by?="space"|"user")` returns `{v, space, group_by, principal,
  range_days, generated_at, totals, series}` with a zero-filled daily series (`totals[m] ==
  sum(series[i][m])`). Two first-class audiences: `group_by="space"` (default) aggregates across ALL
  principals (an aggregate only, `principal: null`); `group_by="user"` is the calling principal alone
  (`principal: <caller>`). No argument can name another principal, so one individual's per-user numbers
  are exposed to nobody but themselves — the non-admin leak is impossible by construction; a privileged
  cross-user breakdown would be a future `space_admin`-gated grain, not built. Increments are best-effort in
  their own post-commit transaction, so a metrics failure never rolls back or fails the operation it
  measures, and never extends the write path's advisory-lock critical section (contention ceiling and
  the bucket-sharding / batched-flush future optimizations documented in the migration).
