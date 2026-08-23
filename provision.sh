#!/bin/sh
# Engraphy: first-run provisioning (POSIX shell mirror of provision.ps1).
#
#   ./provision.sh                          # space "default", principal "me"
#   ./provision.sh myspace alice my-laptop  # space, principal, client name
#
# Waits for /healthz, creates the space (plus its founding space_admin and that
# principal's personal scope), applies the starter pack, mints a scoped
# readwrite bearer token, and prints the exact client settings to paste in.
#
# The token is printed ONCE, here, and is never written to disk by this script.
# The server stores only its SHA-256, so it genuinely cannot be recovered later:
# copy it into your client before closing this window. If you lose it, re-run
# this script -- minting another token is cheap and harmless.
#
# Re-running is safe. An existing space or an already-applied pack is reported
# and skipped rather than treated as a failure, so a re-run still gets a fresh
# token.
#
# Environment overrides (all optional):
#   ENGRAPHY_SPACE       space id                        (default "default")
#   ENGRAPHY_PRINCIPAL   founding principal id           (default "me")
#   ENGRAPHY_CLIENT      client/device name on the token (default "my-client")
#   ENGRAPHY_PACK        pack to apply, path in the container
#                        (default /app/packs/starter/pack.yaml)
#   ENGRAPHY_WAIT_SECS   how long to wait for /healthz   (default 600)
set -eu

cd "$(dirname "$0")"

SPACE="${1:-${ENGRAPHY_SPACE:-default}}"
PRINCIPAL="${2:-${ENGRAPHY_PRINCIPAL:-me}}"
CLIENT_NAME="${3:-${ENGRAPHY_CLIENT:-my-client}}"
# Ships inside the image, so no host path is needed and nothing here is tied to
# where this checkout happens to live.
PACK="${ENGRAPHY_PACK:-/app/packs/starter/pack.yaml}"
WAIT_SECS="${ENGRAPHY_WAIT_SECS:-600}"

if [ ! -f .env ]; then
    echo '[provision] no .env here. Run ./up.sh first.'
    exit 1
fi

PORT="$(sed -n 's/^[[:space:]]*ENGRAPHY_HOST_PORT[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p' .env | tail -1)"
[ -n "${PORT:-}" ] || PORT=8000

# --- 1. wait for the server ------------------------------------------------
# Polls /healthz directly rather than compose's health status: compose reports
# "starting" for up to start_period on first boot, which is indistinguishable
# from a crash-loop from the outside. A 200 is the real signal.
HEALTH="http://127.0.0.1:${PORT}/healthz"
echo "[provision] waiting up to ${WAIT_SECS}s for ${HEALTH}"
deadline=$(( $(date +%s) + WAIT_SECS ))
ok=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 5 "$HEALTH" > /dev/null 2>&1; then ok=1; break; fi
    sleep 5
    echo '  ...still waiting'
done
if [ "$ok" -ne 1 ]; then
    echo "[provision] FAILED: ${HEALTH} did not return 200 within ${WAIT_SECS}s."
    echo '[provision] check the logs with:  docker compose logs --tail 50 engraphy'
    exit 1
fi
echo '[provision] healthz green'

# All three admin verbs run in the `admin` sidecar, whose ENGRAPHY_DATABASE_URL
# is already the superuser connection (see compose.yaml). That is why this
# script never reads or passes a password itself.
admin() {
    docker compose --profile admin run --rm -T admin engraphy-admin "$@"
}

# --- 2. create the space ---------------------------------------------------
echo "[provision] creating space '${SPACE}' (founding space_admin '${PRINCIPAL}')"
if out="$(admin space create --id "$SPACE" --display-name "$SPACE" \
            --principal "$PRINCIPAL" --principal-display-name "$PRINCIPAL" 2>&1)"; then
    echo "$out"
else
    case "$out" in
        *"already exists"*|*"duplicate key"*) echo "  space '${SPACE}' already exists; continuing" ;;
        *) echo "$out"; echo '[provision] FAILED at: space create'; exit 1 ;;
    esac
fi

# --- 3. apply the pack -----------------------------------------------------
echo "[provision] applying pack ${PACK} to '${SPACE}'"
if out="$(admin pack apply "$PACK" --space "$SPACE" 2>&1)"; then
    echo "$out"
else
    case "$out" in
        *"already applied"*|*"already exists"*) echo '  pack already applied; continuing' ;;
        *) echo "$out"; echo '[provision] FAILED at: pack apply'; exit 1 ;;
    esac
fi

# --- 4. mint the token -----------------------------------------------------
echo "[provision] minting a readwrite token for '${PRINCIPAL}'"
if ! out="$(admin token create --space "$SPACE" --principal "$PRINCIPAL" \
              --client-name "$CLIENT_NAME" --role readwrite 2>&1)"; then
    echo "$out"
    echo '[provision] FAILED at: token create'
    exit 1
fi

# The CLI prints a "shown once" preamble line then the raw token on the next
# line. Anchor on that preamble rather than blindly taking the last line, so
# stray compose progress output cannot be mistaken for the token.
TOKEN="$(printf '%s\n' "$out" \
    | sed -e 's/\r$//' -e '/^[[:space:]]*$/d' \
    | awk '/shown once/ { getline; print; exit }')"
if [ -z "${TOKEN:-}" ]; then
    TOKEN="$(printf '%s\n' "$out" | sed -e 's/\r$//' -e '/^[[:space:]]*$/d' | tail -1)"
fi

echo ''
echo '============================================================'
echo ' ENGRAPHY TOKEN -- shown once, not stored anywhere on disk'
echo '============================================================'
echo "$TOKEN"
echo '============================================================'
echo ''
echo 'In VS Code, run the command "Engraphy: Connect to a server" and paste:'
echo "  server URL   http://127.0.0.1:${PORT}/mcp/     (keep the trailing slash)"
echo '  token        the token above'
echo ''
echo 'The extension stores the token in your OS keychain, not in settings.json.'
echo "Optional label (Ctrl+, then search \"engraphy\"):  engraphy.space  ${SPACE}"
echo ''
echo "Your personal scope in this space is: personal-${PRINCIPAL}"
