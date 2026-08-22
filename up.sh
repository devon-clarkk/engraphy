#!/bin/sh
# Engraphy: one-command bring-up (POSIX shell mirror of up.ps1).
#
#   ./up.sh
#
# Generates a .env with fresh random passwords if one does not exist, starts the
# stack, and waits until /healthz answers 200. Safe to re-run: compose is
# idempotent and an existing .env is never overwritten.
#
# Run ./provision.sh afterwards to create a space and mint a client token.
#
# Environment overrides (all optional):
#   ENGRAPHY_HOST_PORT   host port to publish the server on   (default 8000)
#   ENGRAPHY_WAIT_SECS   how long to wait for /healthz        (default 1800)
set -eu

cd "$(dirname "$0")"

WAIT_SECS="${ENGRAPHY_WAIT_SECS:-1800}"

new_secret() {
    # Alphanumeric only, on purpose: these are interpolated into a postgres://
    # URL, where punctuation would need percent-encoding.
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32
}

if [ ! -f .env ]; then
    echo '[up] no .env found; generating one with fresh random passwords'
    {
        printf 'POSTGRES_PASSWORD=%s\n' "$(new_secret)"
        printf 'ENGRAPHY_APP_ROLE_PASSWORD=%s\n' "$(new_secret)"
        printf 'ENGRAPHY_HOST_PORT=%s\n' "${ENGRAPHY_HOST_PORT:-8000}"
    } > .env
    echo '[up] wrote .env (git-ignored; keep it, the database is tied to these passwords)'
else
    echo '[up] using existing .env'
fi

# Read the port back out of .env so the health probe targets the right one.
PORT="$(sed -n 's/^[[:space:]]*ENGRAPHY_HOST_PORT[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p' .env | tail -1)"
[ -n "${PORT:-}" ] || PORT=8000

echo '[up] starting stack (postgres -> init/migrate -> engraphy)...'
if ! docker compose up -d; then
    echo '[up] docker compose up failed (see the output above).'
    echo "[up] if it says 'port is already allocated', set ENGRAPHY_HOST_PORT in .env to a free port and re-run."
    exit 1
fi

HEALTH="http://127.0.0.1:${PORT}/healthz"
echo "[up] waiting up to ${WAIT_SECS}s for ${HEALTH}"
echo '[up] the first run builds the images and downloads the ~523 MB embedding model, so this is slow exactly once'

deadline=$(( $(date +%s) + WAIT_SECS ))
ok=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 5 "$HEALTH" > /dev/null 2>&1; then ok=1; break; fi
    sleep 5
    echo '  ...still waiting'
done

if [ "$ok" -ne 1 ]; then
    echo "[up] FAILED: ${HEALTH} did not return 200 within ${WAIT_SECS}s."
    echo '[up] check the logs with:  docker compose logs --tail 50 engraphy'
    exit 1
fi

echo "[up] healthz green at ${HEALTH}"
echo "[up] MCP endpoint: http://127.0.0.1:${PORT}/mcp/"
echo '[up] next:  ./provision.sh'
