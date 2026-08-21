#!/bin/bash
# Deploy git-http-server to Proxmox LXC 109 (git server) and start via OpenRC
# so the service survives container reboots. Runs as git user.
set -euo pipefail

WS="/home/srv/.picoclaw/workspace/scripts/git_http_server"
CT_SRC="$WS/app.py"
INIT_SRC="$WS/git-http-server.initd"
HOOK_SRC="$WS/post-receive"
MIRROR_SRC="$WS/mirror-sync.sh"
CT_DIR="/opt/git-http-server"
CT="109"
PROX="192.168.2.150"
LOG="/home/srv/.picoclaw/workspace/logs/git_http_deploy.log"

{
echo "=== Deploy git-http-server to container $CT ==="

echo "--- 1. Create dir on container ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- mkdir -p $CT_DIR" 2>&1

echo "--- 2. Copy app.py via stdin ---"
cat "$CT_SRC" | ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'cat > $CT_DIR/app.py'" 2>&1

echo "--- 3. Copy init script + enroll in openrc ---"
cat "$INIT_SRC" | ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'cat > /etc/init.d/git-http-server && chmod +x /etc/init.d/git-http-server'" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- rc-update add git-http-server default" 2>&1

echo "--- 4. Install canonical hooks and per-execution log storage ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'mkdir -p /home/git/hooks /home/git/logs/hooks && chown -R git:git /home/git/hooks /home/git/logs'" 2>&1
cat "$HOOK_SRC" | ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'cat > /home/git/hooks/post-receive && chown git:git /home/git/hooks/post-receive && chmod 0755 /home/git/hooks/post-receive'" 2>&1
cat "$MIRROR_SRC" | ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'cat > /home/git/hooks/mirror-sync.sh && chown git:git /home/git/hooks/mirror-sync.sh && chmod 0755 /home/git/hooks/mirror-sync.sh'" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'for repo in /home/git/repos/*.git; do [ -d \"\$repo\" ] || continue; mkdir -p \"\$repo/hooks\"; ln -sfn /home/git/hooks/post-receive \"\$repo/hooks/post-receive\"; done'" 2>&1

echo "--- 5. Restart service (stops old process, starts via openrc) ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- rc-service git-http-server restart" 2>&1

echo "--- 6. Health check from Proxmox host ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'curl -s http://127.0.0.1:8080/ || wget -qO- http://127.0.0.1:8080/'" 2>&1

echo "--- 7. Process owner check ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'ps aux | grep -E \"python3 /opt/git-http-server/app.py\" | grep -v grep'" 2>&1

echo "== DONE - server under OpenRC, survives reboot =="
} 2>&1 | tee "$LOG"
echo "Log: $LOG"
