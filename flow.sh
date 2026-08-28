#!/bin/bash
# Full flow: create repo via HTTP API, then clone/push over SSH
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="${GIT_SERVER_ENV_FILE:-$SCRIPT_DIR/.env}"
[[ -r "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
: "${GIT_CONTAINER_IP:?Missing GIT_CONTAINER_IP}"
: "${GIT_HTTP_PORT:?Missing GIT_HTTP_PORT}"
: "${GIT_DEFAULT_BRANCH:?Missing GIT_DEFAULT_BRANCH}"
: "${GIT_TEST_SSH_KEY:?Missing GIT_TEST_SSH_KEY}"
WORK="${FLOW_WORK_DIR:-/tmp/git-http-test}"
LOG="${FLOW_LOG:-$SCRIPT_DIR/git_http_flow.log}"

mkdir -p "$WORK"
{
echo "=== [1] Create repo via HTTP API ==="
curl -s -X POST "http://$GIT_CONTAINER_IP:$GIT_HTTP_PORT/create" -H "Content-Type: application/json" -d '{"name":"fluxo-completo"}'
echo

echo "=== [2] Clone over SSH ==="
rm -rf "$WORK/fluxo"
GIT_SSH_COMMAND="ssh -i $GIT_TEST_SSH_KEY -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" \
  git clone "git@$GIT_CONTAINER_IP:repos/fluxo-completo.git" "$WORK/fluxo" 2>&1
cd "$WORK/fluxo"
git branch -a 2>&1

echo "=== [3] Set configured default branch + commit + push ==="
git branch -m "$GIT_DEFAULT_BRANCH" 2>&1
echo "criado via api" > hello-api.txt
git add hello-api.txt
git commit -m "primeiro commit via API" 2>&1
GIT_SSH_COMMAND="ssh -i $GIT_TEST_SSH_KEY -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" \
  git push -u origin "$GIT_DEFAULT_BRANCH" 2>&1

echo "=== [4] Verify remote ==="
GIT_SSH_COMMAND="ssh -i $GIT_TEST_SSH_KEY -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" \
  git ls-remote origin 2>&1

echo "=== DONE ==="
} 2>&1 | tee "$LOG"
echo "Log: $LOG"
