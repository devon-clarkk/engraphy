#!/bin/sh
# One-command bring-up init step (install-friction win #4). Runs in the `init`
# compose service (admin image) AFTER postgres is healthy and BEFORE the engraphy
# server starts, so `docker compose up` brings the whole stack up end-to-end
# with no manual multi-step sequence.
#
# Two steps, in order (both idempotent, so a repeated `up` is a safe no-op):
#   1. migrate  -- create/advance the schema in-process (no dbmate binary), with
#                  the unconditional pre-migration dump migrate always takes.
#   2. provision-- create the non-superuser engraphy_app role and its GRANTs
#                  (including SELECT on schema_migrations, which /healthz needs).
#
# Ordering matters: provision-app-role.sql grants on tables the migrations must
# have created first. ENGRAPHY_DATABASE_URL here is the SUPERUSER connection
# (migrations + role creation need owner rights); the engraphy service connects as
# the far-less-privileged engraphy_app role this script provisions.
set -eu

echo "[init] applying migrations (in-process, no dbmate required)..."
engraphy-admin migrate --dump-dir /backups

echo "[init] provisioning engraphy_app role and grants..."
psql "$ENGRAPHY_DATABASE_URL" \
  --set ON_ERROR_STOP=1 \
  --set app_role_password="$ENGRAPHY_APP_ROLE_PASSWORD" \
  --file /app/deploy/provision-app-role.sql

echo "[init] done -- schema migrated and app role provisioned."
