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

Two items on that list behave differently from the rest, and both matter when
you upgrade:

- **The database name is not renamed by the migration.** `ALTER DATABASE ...
  RENAME TO` cannot run from a session connected to the database it renames, nor
  inside a transaction block, and a migration is both. A *new* install gets the
  `engraphy` name from `compose.yaml` (`POSTGRES_DB: engraphy`); an *existing*
  install keeps the name its volume was initialised with unless the operator
  renames it by hand. That step is optional. Everything works with the database
  still called `engram`, as long as the DSNs say so.
- **`engraphy_sentinel` is stored data, not a schema object.** It is the reserved
  node type the engine registers per space. The migration repoints the rows.
  Anything outside this repo that hard-codes `engram_sentinel` (a pack, a client)
  needs updating to match.

### Upgrading a deployment provisioned before 0024

Read this in full before starting. The role rename means the server cannot
connect with its old DSN, so there is a deliberate stop-the-server window.

1. **Take a backup.** `engraphy-admin migrate` takes an unconditional pre-dump of
   its own, but take your own too, and confirm the file is non-empty before
   continuing.

2. **Attach the same volumes.** Docker Compose derives volume names from the
   project name, which defaults to the directory name. Run compose with the
   *original* project name so it reuses the existing volumes rather than creating
   empty new ones:

   ```bash
   docker compose -p engram up -d      # -p = the original project name
   ```

   The compose project name is unrelated to everything being renamed here, so it
   stays whatever it already was. Confirm with `docker volume ls` and match
   whatever prefixes the existing `*_postgres-data` volume.

3. **Stop the server, leave Postgres running.** The migration renames the role
   the server authenticates as, so a running server would lose its connection
   mid-flight.

4. **Apply the migration** as the superuser. `0024` is safe to run on a
   deployment sitting at any earlier version; it reads the installed catalog and
   rewrites what it finds, rather than assuming a particular applied version.

5. **Re-run `deploy/provision-app-role.sql`** with the same
   `ENGRAPHY_APP_ROLE_PASSWORD`. This is **mandatory, not optional**. Grants are
   held against the role's OID and survive the rename, but a role's *md5*
   password verifier is salted with the role name, so renaming an md5 role blanks
   its password. A SCRAM-SHA-256 verifier (the pg16 default) survives. Rather
   than making you work out which you have, just re-run the script: it is
   idempotent, it resets the password either way, and it re-grants EXECUTE under
   the new function names.

6. **Update every DSN to say `engraphy_app`** instead of `engram_app`, in
   `.env`, unit files, and anything else holding a connection string.

7. **Start the server** and confirm `/healthz` reports the new schema version.

Renaming the database itself is a separate, optional step. It has to happen with
nothing connected to it, from a session attached to the `postgres` maintenance
database:

```sql
ALTER DATABASE engram RENAME TO engraphy;
```

and then update the database component of every DSN to match.

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
