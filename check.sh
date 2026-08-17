#!/bin/bash
# Check git-http-server status on container 109 and test /create endpoint
CT="109"
PROX="192.168.2.150"

echo "=== Server process ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'ps aux | grep -v grep | grep app.py'" 2>&1

echo "=== Server log ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'cat /tmp/git-http.log 2>/dev/null'" 2>&1

echo "=== Health check GET / ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'curl -s http://127.0.0.1:8080/ 2>/dev/null || wget -qO- http://127.0.0.1:8080/ 2>/dev/null'" 2>&1

echo "=== Create repo via POST /create ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'curl -s -X POST http://127.0.0.1:8080/create -H \"Content-Type: application/json\" -d \"{\\\"name\\\":\\\"api-test\\\"}\" 2>/dev/null || wget -qO- --post-data=\"name=api-test\" http://127.0.0.1:8080/create 2>/dev/null'" 2>&1

echo "=== Repos on disk ==="
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$PROX" \
  "pct exec $CT -- sh -c 'ls -la /home/git/repos/'" 2>&1

echo "=== DONE ==="