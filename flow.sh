#!/bin/bash
# Full flow: create repo via HTTP API, then clone/push over SSH
set -euo pipefail
WORK="/tmp/git-http-test"
LOG="/home/srv/.picoclaw/workspace/logs/git_http_flow.log"
CT_IP="192.168.2.163"

mkdir -p "$WORK"
{
echo "=== [1] Create repo via HTTP API ==="
curl -s -X POST "http://$CT_IP:8080/create" -H "Content-Type: application/json" -d '{"name":"fluxo-completo"}'
echo

echo "=== [2] Clone over SSH ==="
rm -rf "$WORK/fluxo"
GIT_SSH_COMMAND="ssh -i /tmp/git-server-test/id_test -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" \
  git clone "git@$CT_IP:repos/fluxo-completo.git" "$WORK/fluxo" 2>&1
cd "$WORK/fluxo"
git branch -a 2>&1

echo "=== [3] Fix branch name (default master -> main) + commit + push ==="
git branch -m main 2>&1
echo "criado via api" > hello-api.txt
git add hello-api.txt
git commit -m "primeiro commit via API" 2>&1
GIT_SSH_COMMAND="ssh -i /tmp/git-server-test/id_test -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" \
  git push -u origin main 2>&1

echo "=== [4] Verify remote ==="
GIT_SSH_COMMAND="ssh -i /tmp/git-server-test/id_test -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" \
  git ls-remote origin 2>&1

echo "=== DONE ==="
} 2>&1 | tee "$LOG"
echo "Log: $LOG"