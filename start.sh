#!/bin/bash
# Start/restart git-http-server on the configured container via OpenRC.
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="${GIT_SERVER_ENV_FILE:-$SCRIPT_DIR/.env}"
[[ -r "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
: "${PROXMOX_HOST:?Missing PROXMOX_HOST}"
: "${PROXMOX_USER:?Missing PROXMOX_USER}"
: "${GIT_CONTAINER_ID:?Missing GIT_CONTAINER_ID}"
PROX_TARGET="$PROXMOX_USER@$PROXMOX_HOST"

echo "=== Restart via OpenRC ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-service git-http-server restart" 2>&1

echo "=== Status ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-service git-http-server status" 2>&1

echo "=== Process ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'ps aux | grep -E \"python3 /opt/git-http-server/app.py\" | grep -v grep'" 2>&1

echo "=== DONE ==="
