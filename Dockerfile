# Engraphy images (design/04 §Deployment shape, cloud profile). Two build targets:
#
#   server (default for the `engraphy` service) -- the running process only.
#   admin  (the `admin` sidecar service)      -- adds the Postgres client tools
#                                                and dbmate for `engraphy-admin
#                                                migrate` / `verify-restore`.
#
# The embedding model (nomic-embed-text-v1.5, on ONNX Runtime) is PRE-BAKED
# into the image at build time (see the prebake RUN below), so first boot is
# offline and instant -- no HuggingFace round trip. The compose `model-cache` volume is
# seeded from that baked cache the first time it is created (Docker seeds an
# empty named volume from the image contents at the mountpoint), so it persists
# the model across rebuilds without ever re-downloading.
FROM python:3.12-slim AS server

# libgomp1: ONNX Runtime's CPU execution provider links against the OpenMP
# runtime and needs it at import time on Debian slim images (pip wheels do not
# carry it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 engraphy
WORKDIR /app

COPY pyproject.toml ./
COPY engraphy ./engraphy
COPY packs ./packs

# Editable installs need the full source tree already copied (above); a
# normal `pip install .` build is used here since the image ships fixed code,
# not a live-edited checkout.
RUN pip install --no-cache-dir .

# Create HF_HOME (and its parent) owned by `engraphy` BEFORE the volume is
# mounted over it. Docker seeds a fresh named volume from the image's contents
# AND ownership at that path -- but only if the path exists in the image. When
# it does not, Docker creates the mountpoint root-owned, and this container
# (USER engraphy, uid 1000) then cannot write its own model cache, crash-looping
# on first boot with:
#   PermissionError: [Errno 13] Permission denied:
#     '/home/engraphy/.cache/huggingface/hub'
# Creating + chowning it here is what makes the named volume come up
# engraphy-owned. Do not move this below `USER engraphy` (it needs root to chown).
RUN mkdir -p /home/engraphy/.cache/huggingface \
    && chown -R engraphy:engraphy /home/engraphy/.cache

USER engraphy
ENV HF_HOME=/home/engraphy/.cache/huggingface

# Prebake the embedding model into the image's HF cache (install-friction win
# #2). Runs the REAL load path -- embedding.embed_document() -> load_model() at
# the pinned MODEL_REVISION -- so the cache ends up holding EXACTLY what runtime
# fetches (the ONNX graph and the tokenizer), not a hand-guessed subset. Runs as
# USER engraphy after the chown, so the baked files are engraphy-owned and the
# model-cache volume is seeded engraphy-owned on first boot. Also fails the build
# fast if the runtime cannot actually execute the graph.
#
# Whichever profile the image defaults to is what gets baked, because this goes
# through the same seam the server does. Building an image for a different
# profile is a build arg away, and the bake follows it automatically.
ARG ENGRAPHY_EMBEDDING_PROFILE
ENV ENGRAPHY_EMBEDDING_PROFILE=${ENGRAPHY_EMBEDDING_PROFILE}
RUN python -c "from engraphy.core import embedding; print('prebaking', embedding.profile()); embedding.embed_document('prebake: warm the model cache')"

# The image's own claim to the name it is published under in the official MCP
# Registry (registry.modelcontextprotocol.io). Ownership of an OCI package is
# proved by this label: the registry pulls the image config for the exact tag
# named in server.json's `packages[].identifier` and requires the value here to
# equal server.json's `name`. The two move together, so a rename is a change to
# both or it does not publish.
LABEL io.modelcontextprotocol.server.name="io.github.devon-clarkk/engraphy"

EXPOSE 8000

# ENGRAPHY_DATABASE_URL is required (no default -- fail fast if unset).
# ENGRAPHY_BIND_HOST defaults to 127.0.0.1 in app.py; the cloud compose profile
# overrides it to 0.0.0.0 explicitly (see compose.yaml) since a container's
# loopback isn't reachable from outside it anyway -- the transport-refusal
# check (app.py::check_transport_security) is what actually gates whether
# that's safe, not this image.
CMD ["python", "-m", "engraphy.server.app"]


# ---------------------------------------------------------------------------
# admin: the operator sidecar (compose service `admin`, profile "admin").
#
# `engraphy-admin migrate` and `verify-restore` shell out to pg_dump/pg_restore
# and dbmate. The server image deliberately omits those, and requiring them on
# the HOST made the cloud profile unrunnable on machines without the Postgres
# client tools installed (Windows especially). Shipping them in a sidecar keeps
# migrate's unconditional pre-dump -- the safety property design/04 asks for --
# instead of adding a skip flag to work around a missing binary.
#
# Runs one-shot: `docker compose run --rm admin engraphy-admin <verb> ...`.
# It reaches postgres over the compose network, so the postgres service needs
# no published host port. See deploy/checklist.md's cloud section.
#
# The Postgres client is PINNED to 16, matching compose's `pgvector/pgvector:pg16`
# server. Debian's unversioned `postgresql-client` metapackage tracks the distro's
# newest (pg17 on trixie), and a newer pg_restore is NOT backward compatible in
# the direction that matters here: pg_restore 17 emits `SET transaction_timeout = 0`
# in its prologue -- a GUC that does not exist before 17 -- so restoring into the
# pg16 server fails with "unrecognized configuration parameter". Found 2026-07-21
# by the first real backup/restore drill; `verify-restore` was unusable on the
# shipped cloud profile because of it. CI's `test` job already pins
# postgresql-client-16 for exactly this reason (.github/workflows/ci.yml, with an
# assertion) -- the same drift went unnoticed here because `deploy-smoke` only ever
# runs `migrate` (pg_dump), never a restore. If the compose Postgres major is ever
# bumped, this pin and that CI pin move together.
FROM server AS admin
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && pg_restore --version | grep -q ' 16\.' \
    && curl -fsSL -o /usr/local/bin/dbmate \
         https://github.com/amacneil/dbmate/releases/latest/download/dbmate-linux-amd64 \
    && chmod +x /usr/local/bin/dbmate \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# deploy/ carries provision-app-role.sql, which the checklist runs through this
# sidecar's psql (the server image has no reason to carry it).
COPY deploy ./deploy

# Same shape as the HF_HOME fix in the server stage above, and for exactly the
# same reason: create the dump target owned by `engraphy` BEFORE the volume is
# mounted over it, so Docker seeds the named `backups` volume with that
# ownership. Without this the mountpoint is created root-owned and the sidecar
# (USER engraphy, uid 1000) cannot write its pre-migrate dump:
#   pre-dump failed: pg_dump: error: could not open output file
#     "/backups/pre-migrate-<ts>.pgdump": Permission denied
# This is also why /backups is a named volume rather than a `./backups` bind
# mount (see compose.yaml): a bind mount's ownership comes from the HOST
# directory, which on a fresh checkout either does not exist (Docker then
# creates it root-owned) or is owned by whatever uid did the checkout -- uid
# 1000 on a typical workstation, but NOT on a CI runner. Seeding from the image
# is the only variant that is correct on both. Needs root, so it stays above
# the `USER engraphy` line.
RUN mkdir -p /backups && chown engraphy:engraphy /backups

# No PYTHONPATH override here, deliberately. An earlier revision set
# PYTHONPATH=/app so the CLI would resolve the copied source tree, because the
# admin verbs locate data files relative to their own module
# (packs.py -> parents[2]/packs/schema.json, migrate.py -> parents[1]/db/
# migrations) and those files were not installed by `pip install .`. That is
# now fixed properly at the packaging layer -- pyproject.toml ships them as
# package data -- so the installed package finds its own assets and this image
# exercises the same artifact a `pip install` operator gets, rather than
# masking a packaging gap with a path override.

# The admin sidecar inherits every LABEL from the `server` stage, including the
# MCP Registry name claim. It is a Postgres-client and migration toolbox rather
# than the MCP server, so it gives that claim up here: the published
# ghcr.io/devon-clarkk/engraphy-admin image carries an empty value, and
# ghcr.io/devon-clarkk/engraphy is the only image that answers to the registry
# name.
LABEL io.modelcontextprotocol.server.name=""

USER engraphy
CMD ["engraphy-admin", "--help"]
