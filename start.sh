#!/bin/bash
# Start git-http-server on container 109 AS git USER (so created repos are owned by git)
CT="109"
PROX="192.168.2.150"

echo "=== Stop any existing server ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'pkill -f app.py 2>/dev/null; sleep 1; true'" 2>&1

echo "=== Start server as git user (setsid, detached) ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'cd /opt/git-http-server && chown -R git:git /opt/git-http-server && chown git:git /home/git/repos && su -s /bin/sh git -c \"setsid sh -c \\\"nohup python3 app.py >/opt/git-http-server/server.log 2>&1 </dev/null &\\\"\" && sleep 1 && cat /opt/git-http-server/server.log'" 2>&1

echo "=== Process check ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'ps aux | grep -v grep | grep app.py'" 2>&1

echo "=== Health check ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'curl -s http://127.0.0.1:8080/ 2>/dev/null || wget -qO- http://127.0.0.1:8080/ 2>/dev/null'" 2>&1

echo "=== DONE ==="