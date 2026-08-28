#!/bin/bash
# Check the configured git-http-server container and test /create.
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="${GIT_SERVER_ENV_FILE:-$SCRIPT_DIR/.env}"
[[ -r "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
: "${PROXMOX_HOST:?Missing PROXMOX_HOST}"
: "${PROXMOX_USER:?Missing PROXMOX_USER}"
: "${GIT_CONTAINER_ID:?Missing GIT_CONTAINER_ID}"
: "${GIT_HTTP_PORT:?Missing GIT_HTTP_PORT}"
PROX_TARGET="$PROXMOX_USER@$PROXMOX_HOST"

echo "=== Server process ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'ps aux | grep -v grep | grep app.py'" 2>&1

echo "=== Server log ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat /opt/git-http-server/server.log 2>/dev/null'" 2>&1

echo "=== Health check GET / ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'curl -s http://127.0.0.1:$GIT_HTTP_PORT/ 2>/dev/null || wget -qO- http://127.0.0.1:$GIT_HTTP_PORT/ 2>/dev/null'" 2>&1

echo "=== Create repo via POST /create ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'curl -s -X POST http://127.0.0.1:$GIT_HTTP_PORT/create -H \"Content-Type: application/json\" -d \"{\\\"name\\\":\\\"api-test\\\"}\" 2>/dev/null || wget -qO- --post-data=\"name=api-test\" http://127.0.0.1:$GIT_HTTP_PORT/create 2>/dev/null'" 2>&1

echo "=== Repos on disk ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'ls -la /home/git/repos/'" 2>&1

echo "=== DONE ==="
