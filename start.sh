#!/bin/bash
# Start/restart git-http-server on container 109 via OpenRC (survives reboot).
set -euo pipefail
CT="109"
PROX="192.168.2.150"

echo "=== Restart via OpenRC ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- rc-service git-http-server restart" 2>&1

echo "=== Status ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- rc-service git-http-server status" 2>&1

echo "=== Process ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'ps aux | grep -E \"python3 /opt/git-http-server/app.py\" | grep -v grep'" 2>&1

echo "=== DONE ==="