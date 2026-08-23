# Engraphy — Build Roadmap

Phased build order with acceptance criteria. Engraphy development needs only a laptop and a scratch Postgres.

**Status:** Living document
**Last updated:** July 2026

---

## Repo layout

```
Engraphy/
├── design/                  # these documents (authoritative)
├── engraphy/
│   ├── db/migrations/       # dbmate SQL (01)
│   ├── core/                # search, briefing, traverse, dedup, embedding (02)
│   ├── server/              # FastMCP app, tools, aliases, auth (03)
│   ├── admin/               # engraphy-admin verbs (03, 04)
│   └── tests/
├── packs/starter/pack.yaml  # shipped default pack (01) — committed
├── packs/schema.json        # extracted verbatim from design/07 (both packs validate)
├── deploy/                  # checklist, clients guide, unit files (04)
├── scripts/                 # baseline_dedup_fixtures.py etc.
├── COMPATIBILITY.md, CHANGELOG.md, DECISIONS-DELTA.md, QUESTIONS.md
└── README.md
```

The example pack is committed at `engraphy/tests/fixtures/packs/example-pack.yaml` so CI proves the pack mechanism standalone, against its most demanding form, without any external repo.

## Implementer ground rules

The ground rules (docs win; criteria are executed, not eyeballed; secrets never in-repo) plus two: **every feature lands with both shipped packs passing** — a change that works for starter but breaks the example pack (or vice versa) is not done — and **[07-implementation-contracts.md](07-implementation-contracts.md) is normative**: fixtures before code, formulas and wire shapes byte-exact, gaps go to `DECISIONS-DELTA.md`/`QUESTIONS.md` per its deviation protocol, never into silent invention. Module order within phases is 07's, and each E-phase below implicitly includes its 07 fixture deliverables.

---

## Phase E0 — Schema and enforcement kernel

**Docs:** [01](01-core-data-model.md); component plans: [attr-spec](implementation/attr-spec-interpreter-plan.md), [visibility/RLS](implementation/visibility-and-rls-plan.md) — follow their internal build orders verbatim.
Deliverables: migrations (stubs 0001–0009 committed, headers state contents); attr-spec interpreter (both implementations + parity fuzz); the three triggers; RLS (FORCE, definer functions, policies per the plan); `pack validate|apply`; both packs applying from empty; the full constraint + isolation + concurrency test suite.

**Acceptance criteria:** [01's list](01-core-data-model.md#acceptance-criteria), verbatim. Exit gate: the RLS probe and the raw-SQL constraint tests are green in CI, not just locally.

## Phase E1 — Engine behaviors

**Docs:** [02](02-retrieval-and-dedup.md); component plan: [dedup write path](implementation/dedup-write-path-plan.md) — its build order and race/crash tests are the E1 core.
Deliverables: embeddings (run `scripts/baseline_dedup_fixtures.py` once the model lands), hybrid search, briefing engine, traversal, dedup bands + pending + resonance, inbox, bulk import + review queue.

**Acceptance criteria:** [02's list](02-retrieval-and-dedup.md#acceptance-criteria), verbatim.

## Phase E2 — Server, auth, admin

**Docs:** [03](03-api-auth-and-tenancy.md)
Deliverables: FastMCP server, tokens/roles/rate limits, aliases + description assembly, audit, `engraphy-admin` complete, `/inbox`, `/healthz`.

**Acceptance criteria:** [03's list](03-api-auth-and-tenancy.md#acceptance-criteria), verbatim — the three-client round-trip and the cross-space fuzz suite are the gate.

## Phase E3 — Productization

**Docs:** [04](04-operations-and-governance.md)
Deliverables: `migrate`/`verify-restore`/`doctor`/`pack upgrade`, deploy docs **for both profiles** (local/overlay + cloud: Dockerfile, compose, TLS refusal), COMPATIBILITY/CHANGELOG, release checklist run for **v0.1.0** (tagged), throwaway-VM cloud stand-up test.

**Acceptance criteria:** [04's list](04-operations-and-governance.md#acceptance-criteria), verbatim.

## Phase E3.5 — the E4-entry hardening gate (added 2026-07-20, per E3-REVIEW.md)

E3's acceptance list was declared complete with the cloud stand-up effectively unexecuted (a provisional v0.1.0 tag existed locally at the time; it was never pushed and no longer exists — see the merge row below); the first live operator walkthrough (2026-07-20) then found twelve deploy-packaging findings — all engine-external, all since fixed and **proven by a clean from-scratch stand-up** (healthcheck green, no crash-loop, zero password echo, migrate via the admin sidecar, 10/10 MCP round trip including a 0.99 dedup merge). E3.5 names the remaining preconditions for crossing E4's one-way door, so "through E3 everything is scratch" stays true until they hold:

- [x] All walkthrough findings fixed and re-proven end-to-end (done 2026-07-20).
- [x] Branch pushed to a durable remote (done 2026-07-20; `origin/phase/e3` at github.com/devon-clarkk/Engraphy).
- [x] Fold-back: DECISIONS-DELTA/QUESTIONS resolutions folded into design/01–08 so the normative docs describe the built system (this commit series, 2026-07-20).
- [x] Working tree moved off sync-managed storage (done 2026-07-21 — the repo now lives at a non-OneDrive path; the synced copy is stale and demoted to "never touch"). QUESTIONS.md "repo-environment" is satisfied.
- [x] CI deploy-smoke job + Windows CLI lane green (run 29763088585 on 5b418f6: `test`, `deploy-smoke`, `windows-cli` all pass; deploy-smoke's steps 4–8 executed for the first time — sidecar migrate 0001→0017, role provisioning, healthcheck green in ~30s, space+pack+token, 9/9 MCP round trip incl. a 0.99 merge).
- [x] One real backup/restore drill on the deployment shape (done 2026-07-21). `pg_dump --format=custom` taken from the populated compose instance, `verify-restore` run against it — restore into a freshly-created scratch DB, `schema_migrations` at 0017, per-space row counts, all three constraint probes firing against real constraints, and the sentinel **self-located from config and content-matched** for the space minted under the convention, with the two pre-convention spaces skipped-with-log exactly as designed. Then an **independent** restore into a separate clean database, kept and inspected rather than dropped: the 0.99-merged node came back with identical title, body length and addendum (md5-equal), and all 13 tables matched the source row-for-row. The drill found a real shipped bug — see the [04](04-operations-and-governance.md) client-version pin — so it earned its place rather than rubber-stamping. Still operator setup, carried to E4: the *cadence* (cron/timer) and `/healthz.last_backup_at` population on a live deployment.
- [x] `phase/e3` merged to `main` (2026-07-21, fast-forward `a263dfa`→`d893f0a`, 122 commits, pushed). **The tag is deliberately not part of this gate** (ruling 2026-07-21): no version tag is cut until the first production release, so the "re-cut v0.1.1" wording is withdrawn — v0.1.0 never existed on any remote and no tag exists in the repo. `RELEASE-CHECKLIST.md`'s v0.1.0 record stands as the record of the *checklist run*, not of a tag.
- [x] Sentinel convention implemented **and verified** (2026-07-21). Mint at `space create`, `sentinel.node_id` config key, self-locating `verify-restore`, reserved-name refusal in `pack validate`, plus the type-exclusions in search/briefing and the `doctor`/`pack upgrade` exemptions the spec did not anticipate. `engraphy/tests/test_sentinel.py` — 15 tests — passes against a real Postgres, and the full suite is green (846 passed / 4 skipped, migrations from empty), so the read-path exclusions disturbed no golden fixture. Proven end-to-end by the drill above, not merely unit-tested: a sentinel minted on the cloud profile survived a real dump/restore and was found from config alone.

**Gate status (2026-07-21): CLOSED.** Every row above is met and the last outstanding verification has landed. The drill exposed a genuine shipped defect — the admin sidecar's Postgres client was unpinned and resolved to 17 against a pg16 server, breaking `pg_restore` on the cloud profile ([04](04-operations-and-governance.md) §Backup contract). The fix (pin + a CI step that actually restores a dump) touched a Dockerfile layer, so it needed a real build to be believed. **CI run [29803411868](https://github.com/devon-clarkk/Engraphy/actions/runs/29803411868) on `e759198` provides it** — `test`, `deploy-smoke` and `windows-cli` all green, and confirmed from the job log rather than the checkmark: the admin image built `postgresql-client-16` (16.14-1.pgdg13+1) from PGDG, the in-layer `pg_restore --version` assertion held, and the new drill step dumped and restored into a scratch database, logging `space 'smoke': sentinel '72a7f005-…' retrieved from config (status 'archived', content matches)`. That sentinel was minted by CI's own `space create` on a freshly built image and located from config alone after a real dump/restore — the convention proven end-to-end by machinery nobody hand-held.

**The caveat is unchanged and still binding: this proves the restore path on the *cloud/compose* profile only.** The local/overlay profile has still never been stood up — its systemd/launchd units are shipped but unexecuted, and neither Windows nor CI's containers can run them ([04](04-operations-and-governance.md) §Acceptance criteria says so explicitly). Nothing here licenses an assumption that an overlay deployment can restore; first real overlay run is E4.

*History note for whoever reads this later.* The float4 tolerance defect in `test_sentinel.py` (asserting `rel_tol=1e-9` against pgvector's single-precision `vector` storage, which round-trips `1/√384` with ~3.6e-8 relative error) was caught by a static review pass — but only **after** it had already turned CI red twice, on runs `29793430641` and `29793640374`, where it surfaced as a bare `assert False` and read exactly like a broken mint rather than a bad assertion. Fixed in `cdb5e25`. The transferable lesson is not about floats: code written with no reachable database accumulates defects that look like *engine* bugs when they finally run, so the static pass before the first execution is worth its cost — and the CI history is the honest record of the order those two things happened in.

Small build items that fall out of the 2026-07-20 design decisions, scheduled here or early E4 (all pre-E5): the sentinel convention and its `pack validate` reserved-name check (both now tracked as the gate row above, since the backup drill is what wants them), `purge-session` ([04](04-operations-and-governance.md) §Hygiene — **must land before E5's backfill**), and pack upgrade's retire semantics (`retired_at` — [04](04-operations-and-governance.md) §Pack migrations is redesigned on paper, the code still removes the registry row).

**E4-entry items ruled 2026-07-21 (build before crossing, not early-E4 work).** Two wire-contract changes were ruled after the gate closed; both change what the server *accepts*, which is an edit today and a breaking change the moment E4 gives the contract real consumers — so they land before E4, and neither reopens E3.5 (that gate closed on what was ruled then): (1) the reserved-type write-path refusal — `engraphy_sentinel` on `write`/`supersede`/import/promote → `ENGRAPHY_VALIDATION` (QUESTIONS "sentinel-write-path-refusal" carries the spec); (2) dispatcher-side enforcement of [07](07-implementation-contracts.md)'s per-argument wire types, with the published `inputSchema` generated from the same spec and the SDK's own validation path permanently off (07 §Per-argument wire types carries the pinned semantics). Both are one-session builds; neither touches a golden wire fixture. **(3) (added later on 2026-07-21, per the dupstream contradiction finding)** the `merged` envelope's `instruction` field plus the `write` base-description sentence naming the supersede repair loop ([02](02-retrieval-and-dedup.md) §What auto-merge cannot see; 07's amended merged envelope) — this one DOES touch a golden fixture (`write_merged.json`, additive field, design-owner edit). The caller-responsibility contract must be on the wire before the first real consumers code against it; the client-side counterpart (a session protocol instructing the merged-envelope check) is an E4 adoption-readiness item, not an engine build.

## Phase E4 — first-deployment adoption

Deploy v0.1 to a real self-hosted or cloud host; `space create nova`; apply the example pack; mint seat tokens; point an external backup scheduler at the Engraphy database; wire `verify-restore` into that scheduler's monthly job.

**Acceptance criteria**

- [ ] The full acceptance surface across [01](01-core-data-model.md)/[02](02-retrieval-and-dedup.md)/[03](03-api-auth-and-tenancy.md) passes against Engraphy + the example pack.
- [ ] The session-protocol scenarios run unchanged except tool-name aliases.
- [ ] The **production declaration** happens here: real memory begins on Engraphy, never on a pre-production prototype.

## Phase E5 — Second space: Alex (the multi-tenancy proof)

The multi-space design is proven by a real second human, not a fixture: `space create alex` + starter pack; his devices tokened per `deploy/clients.md` (plan prerequisites verified first); a conversation-history backfill via the import pipeline with a reviewed queue; the scheduled-recap capture pattern running.

**Entry prerequisite (added 2026-07-20):** `purge-session` is built and tested before the backfill runs — a ≥200-item import of machine-extracted content is exactly the "untraceable bad batch" scenario the verb exists for ([04](04-operations-and-governance.md) §Hygiene carries its decided semantics), and running the backfill without the cleanup tool is accepting that risk with no exit. **Backfill caveat (added 2026-07-21):** import-mode auto-merge absorbs silently with nobody in the loop, so a chronologically-ordered conversation export **destroys its own corrections** — the later "actually, X changed" lines merge into the facts they overturn ([02](02-retrieval-and-dedup.md) §What auto-merge cannot see). The exporter must emit supersede intent for corrections, or the backfill accepts that loss knowingly; `ImportSummary.merged_addendum_dropped` is the after-the-fact tell to check.

**Acceptance criteria**

- [ ] Alex's Desktop and phone reach his space; his briefing works; his memory demonstrably grows over two weeks of normal use without Devon intervening.
- [ ] The isolation fuzz suite re-run against the **live** instance with both spaces populated.
- [ ] One real backfill (≥ 200 items) imported: review queue triaged, re-import is a no-op.
- [ ] Alex's onboarding used `deploy/clients.md` alone — gaps found become doc fixes, not tribal knowledge.

## Phase E6 — Team space (the sharing proof)

**Docs:** [06](06-teams-and-sharing.md)
Tenancy-v2 columns and functions exist from E0 (they're in the base DDL — single-principal spaces just don't exercise them), but the *sharing behaviors* — visibility filtering in every read path, edge both-endpoint rules, dedup privacy, space-admin tools — are implemented and hardened here, after single-principal operation (E4/E5) is proven. Pilot: a real two-person team space (two collaborators on a shared project), on whichever profile fits (cloud VM if a collaborator isn't tailnet-reachable).

**Acceptance criteria:** [06's list](06-teams-and-sharing.md#acceptance-criteria), verbatim — the three scenario stories, the single-principal-reduction regression, and the re-run cross-space fuzz are the gate.

**Entry checklist (added 2026-07-20)** — three deliberately-deferred single-principal shortcuts that must be revisited *by plan* here, not rediscovered as bugs: (1) owner self-service on `admin_scope_visibility`/`admin_grant` (shipped space_admin-only — [06](06-teams-and-sharing.md) §Space administration); (2) personal-scope auto-creation for members added via `admin_member_add` ([06](06-teams-and-sharing.md) §Personal scopes); (3) the recall-stats bump on a readable-but-not-writable node (the nodes UPDATE policy's `WITH CHECK(writable)` fails it — harmless while readable == writable, real once read-only grants exist; DECISIONS-DELTA 2026-07-16).

---

## Dependency graph

```
E0 (kernel) → E1 (behaviors) → E2 (server) → E3 (v0.1.0) → E4 (first deployment) → E5 (second space) → E6 (team pilot)
```

## Rollback posture

Through E3.5 everything is scratch. E4 is the one-way door (the production declaration — the same door, relocated): after it, data is forever (no-hard-deletes against a live corpus), wire envelopes and error strings have real consumers, the embedding model + task prefixes are locked to a production corpus, and migrations become append-only in reality. E3.5 exists because those are exactly the things that are cheap to change now and a project to change later. E5 adds a second space behind the same guarantees; E6 adds principals *within* a space behind the visibility model. Removing a space (`engraphy-admin space archive`) and archiving a principal are supported and non-destructive.
