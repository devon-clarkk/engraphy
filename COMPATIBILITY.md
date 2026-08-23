# Compatibility

## Naming: nothing is called `engram` any more

The project was renamed from **Engram** to **Engraphy**. For a while the rename
stopped at the database boundary: a small set of database-internal identifiers
deliberately kept the `engram` prefix, because renaming them would have broken an
already-provisioned Postgres cluster, and this file documented them as frozen.

**That freeze is lifted.** Migration `0024_engraphy_identifier_rename.sql`
renames every one of them. The project is young enough that a single
operator-visible upgrade step costs less than carrying two names forever.

| Was | Is now |
|-----|--------|
| SQL functions `engram_readable_scopes()`, `engram_writable_scopes()`, `engram_validate_attrs()`, `engram_addenda_text()` | `engraphy_*()` |
| Session GUCs `engram.space_id`, `engram.principal` | `engraphy.space_id`, `engraphy.principal` |
| Postgres role `engram_app` | `engraphy_app` |
| Reserved node type `engram_sentinel` | `engraphy_sentinel` |
| Database `engram` | `engraphy` |

One item on that list behaves differently from the rest, and one is not done by
the migration at all:

- **`engraphy_sentinel` is stored data, not a schema object.** It is the reserved
  node type the engine registers per space. The migration repoints the rows.
  Anything outside this repo that hard-codes `engram_sentinel` (a pack, a client)
  needs updating to match.
- **The database name is renamed by the operator, not by the migration.**
  `ALTER DATABASE ... RENAME TO` cannot run from a session connected to the
  database it renames, nor inside a transaction block, and a migration is both.
  A new install gets the name from `compose.yaml` (`POSTGRES_DB: engraphy`). An
  existing install keeps whatever name its volume was initialised with, so the
  rename is a step in the sequence below. It is **not optional on the compose
  path**: the shipped `compose.yaml` hardcodes `/engraphy` in all three DSNs and
  exposes no knob for the database name, so an existing volume still holding a
  database called `engram` would send the init sidecar at a database that does
  not exist.

### Upgrading a deployment provisioned before 0024

Read this in full before starting. Two things make this more than a `migrate`:
the role the server authenticates as is renamed, and the server's boot-time
schema gate compares applied against expected **for equality**
(`app.py::check_schema_version`), so an image built before 0024 refuses to start
against a database that has it. The new image and the migration have to land
together.

The order below exists so that each step's prerequisite is already true when it
runs. `deploy/compose-init.sh` runs `migrate` and *then*
`provision-app-role.sql`, which is what lets steps 4 and 5 happen in one
`docker compose up` with no manual DSN editing in between.

1. **Take a backup and check it.** `engraphy-admin migrate` takes an
   unconditional pre-dump of its own, but take your own too and confirm the file
   is non-empty before continuing. Note that a dump taken *before* 0024 restores
   a pre-rename schema: if you ever restore one, 0024 has to be applied to it
   again afterwards.

2. **Stop the app, leave Postgres running.** Use `stop`, never `down -v`, and
   pass the original project name so the existing volumes are reused:

   ```bash
   docker compose -p engram stop
   ```

   The compose project name is unrelated to everything being renamed here, so it
   stays whatever it already was. Confirm with `docker volume ls` that it matches
   whatever prefixes the existing `*_postgres-data` volume.

3. **Rename the database**, with nothing connected to it, from a session
   attached to the `postgres` maintenance database:

   ```sql
   ALTER DATABASE engram RENAME TO engraphy;
   ```

4. **Bring the stack up on the new code**, rebuilding the images so the server
   carries the 0024 migration its boot gate expects:

   ```bash
   docker compose -p engram up -d --build
   ```

   The init sidecar now migrates up to 0024 against the renamed database, and
   0024 renames `engram_app` to `engraphy_app` as part of that.

5. **The role password and grants are re-asserted for you.** init runs
   `deploy/provision-app-role.sql` immediately after migrate, which matters more
   than it looks: grants are held against the role's OID and survive the rename,
   but a role's *md5* password verifier is salted with the role name, so renaming
   an md5 role blanks its password. A SCRAM-SHA-256 verifier (the pg16 default)
   survives. Re-running the script resets the password either way and re-grants
   EXECUTE under the new function names, so you never have to work out which
   verifier you had.

6. **Confirm.** `/healthz` should report `"schema_version":"0024"`. If the server
   crash-loops with a `SchemaVersionMismatch`, its image predates 0024 and step 4
   did not actually rebuild it.

On a non-compose install (systemd, launchd, a hand-rolled DSN), the same sequence
applies, with steps 4 and 5 done by hand: `engraphy-admin migrate`, then
`psql ... -f deploy/provision-app-role.sql`, then update every
`ENGRAPHY_DATABASE_URL` to say `engraphy_app` and `/engraphy` before restarting.

---

Engine version ↔ pack format version ↔ migration floor (design/04
s.Versioning and release discipline). "Pack format version" is the pack
FILE FORMAT (`packs/schema.json`'s own shape, `engraphy/admin/packs.py`'s
`CURRENT_PACK_FORMAT`), distinct from a shipped pack's own `version:` field
(its content revision). "Migration floor" is the lowest `schema_migrations`
version this engine version can `engraphy-admin migrate` up from; below the
floor, restore from a dump and replay forward, or upgrade through an
intermediate release first.

| Engine version | Pack format version | Migration floor | Notes |
|----------------|--------------------|-----------------|-------|
| 0.1.0          | 1                  | 0001            | First tagged release. Engine version bump = semver (major = tool-signature/DDL-contract breaks; minor = new tools/constructs; patch = fixes). |
| (unreleased)   | 1                  | 0001            | Pre-v0.1.0 development |

## Pack-format warning

`engraphy-admin pack apply` / `pack upgrade` print a warning (not a hard
failure) when a pack declares `pack_format` greater than this engine's
`CURRENT_PACK_FORMAT`: a pack authored for a newer engine, applied against
an older one. This is a narrower check than design/04's literal wording
("warns when a pack uses constructs newer than it declares") would suggest,
which describes a per-construct staleness check that would need every
`packs/schema.json` addition tagged with the format version that introduced
it; nothing in this codebase tracks that yet. See
`engraphy/admin/packs.py::check_pack_format`'s docstring for the full reasoning.
