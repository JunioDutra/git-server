#!/bin/bash
# Deploy git-http-server app.py to Proxmox LXC 109 (git server) and start it.
set -euo pipefail

CT_SRC="/home/srv/.picoclaw/workspace/scripts/git_http_server/app.py"
CT_PATH="/opt/git-http-server/app.py"
CT="109"
PROX="192.168.2.150"
LOG="/home/srv/.picoclaw/workspace/logs/git_http_deploy.log"

{
echo "=== Deploy git-http-server to container $CT ==="
echo "--- 1. Create dir on container ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- mkdir -p /opt/git-http-server" 2>&1

echo "--- 2. Copy app.py via Proxmox host (pct push) ---"
# Push file to container using pct push
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct push $CT $CT_SRC $CT_PATH" 2>&1 || true
# Alternative: push via stdin using pct exec + cat
cat "$CT_SRC" | ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" "pct exec $CT -- sh -c 'cat > /opt/git-http-server/app.py'" 2>&1

echo "--- 3. Verify file on container ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'ls -la /opt/git-http-server/app.py && head -3 /opt/git-http-server/app.py'" 2>&1

echo "--- 4. Start server as git user (port 8080, repos root /home/git/repos) ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'pkill -f git_http_server 2>/dev/null; cd /opt/git-http-server && nohup python3 app.py > /tmp/git-http.log 2>&1 & sleep 1; cat /tmp/git-http.log'" 2>&1

echo "--- 5. Health check from Proxmox host ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'curl -s http://127.0.0.1:8080/ || wget -qO- http://127.0.0.1:8080/'" 2>&1

echo "=== DONE ==="
} 2>&1 | tee "$LOG"
echo "Log: $LOG"