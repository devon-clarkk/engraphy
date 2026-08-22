# Compatibility

## Naming: Engram → Engraphy, and the identifiers that stay `engram`

The project was renamed from **Engram** to **Engraphy**. The rename is complete
across the Python package (`engraphy`), the CLI (`engraphy-admin`), the Docker
compose service, the config environment variables (`ENGRAPHY_*`), the MCP tool
error codes (`ENGRAPHY_*`), and all user-facing text.

A deliberately small set of **database-level identifiers keeps the `engram`
prefix**, because they live *inside* a provisioned Postgres cluster and renaming
them would break an existing deployment's data:

- the **database name** `engram`;
- the application **role** `engram_app` (the `NOBYPASSRLS` role the server
  connects as; RLS policies and grants are written against it);
- the **SQL functions** `engram_readable_scopes()`, `engram_writable_scopes()`,
  `engram_validate_attrs()`;
- the **session GUCs** `engram.space_id` and `engram.principal` used by RLS.

These are frozen in the migration SQL (`engraphy/db/migrations/*.sql`), which is
append-only and never edited after it ships.

### Upgrading an existing Engram deployment to Engraphy

No data migration is required. The database, role, functions, and GUCs are
unchanged, so the existing Postgres volume is used as-is. Two things change:

1. **Attach the same volumes.** Docker Compose derives volume names from the
   project name, which defaults to the directory name. Run compose with the
   *original* project name so it reuses the existing volumes rather than creating
   empty new ones:

   ```bash
   docker compose -p engram up -d      # -p = the original project name
   ```

   (If the old stack ran from a directory named `Engram`, its project name is
   `engram`; confirm with `docker volume ls` and match whatever prefixes the
   existing `*_postgres-data` volume.)

2. **Rename the environment variables.** In your `.env` / unit environment,
   `ENGRAM_*` becomes `ENGRAPHY_*` (`ENGRAM_DATABASE_URL` → `ENGRAPHY_DATABASE_URL`,
   `ENGRAM_APP_ROLE_PASSWORD` → `ENGRAPHY_APP_ROLE_PASSWORD`, etc.). The server
   fails loudly if a required one is missing, so a missed rename is obvious, not
   silent. The DSN *values* still point at database `engram` and role
   `engram_app` — that is intentional.

---

Engine version ↔ pack format version ↔ migration floor (design/04
s.Versioning and release discipline). "Pack format version" is the pack
FILE FORMAT (`packs/schema.json`'s own shape, `engraphy/admin/packs.py`'s
`CURRENT_PACK_FORMAT`) — distinct from a shipped pack's own `version:` field
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
`CURRENT_PACK_FORMAT` — a pack authored for a newer engine, applied against
an older one. This is a narrower check than design/04's literal wording
("warns when a pack uses constructs newer than it declares") would suggest,
which describes a per-construct staleness check that would need every
`packs/schema.json` addition tagged with the format version that introduced
it; nothing in this codebase tracks that yet. See
`engraphy/admin/packs.py::check_pack_format`'s docstring for the full reasoning.
