#!/bin/bash
# Deploy git-http-server and canonical hooks to the configured Proxmox LXC.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="${GIT_SERVER_OPS_ENV_FILE:-$SCRIPT_DIR/.env}"
if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

required=(PROXMOX_HOST PROXMOX_USER GIT_CONTAINER_ID GIT_HTTP_PORT)
missing=()
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || missing+=("$name")
done
if ((${#missing[@]})); then
  echo "Missing required environment variables: ${missing[*]}" >&2
  exit 2
fi

PROX_TARGET="$PROXMOX_USER@$PROXMOX_HOST"
CT_DIR="/opt/git-http-server"
DEPLOY_LOG="${DEPLOY_LOG:-$SCRIPT_DIR/git_http_deploy.log}"

{
echo "=== Deploy git-http-server to container $GIT_CONTAINER_ID ==="

echo "--- 1. Validate inherited container environment ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'missing=; for name in GIT_HTTP_HOST GIT_HTTP_PORT GIT_SSH_HOST GIT_DEFAULT_BRANCH REGISTRY_ADDRESS REGISTRY_USER REGISTRY_PASSWORD REGISTRY_INSECURE BUILDKIT_ADDRESS BUILDX_BUILDER; do value=\$(printenv \"\$name\"); [ -n \"\$value\" ] || missing=\"\$missing \$name\"; done; [ -z \"\$missing\" ] || { echo \"Missing required container environment variables:\$missing\" >&2; exit 2; }'" 2>&1

echo "--- 2. Install runtime dependencies and directories ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- apk add --no-cache python3 py3-yaml" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'mkdir -p $CT_DIR /home/git/hooks /home/git/logs/hooks && chown -R git:git $CT_DIR /home/git/hooks /home/git/logs'" 2>&1

echo "--- 3. Stage application, service, and hooks ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > $CT_DIR/app.py.new'" < "$SCRIPT_DIR/app.py"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > /etc/init.d/git-http-server.new'" < "$SCRIPT_DIR/git-http-server.initd"
for file in post-receive mirror-sync.sh build_image.py; do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
    "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > /home/git/hooks/$file.new'" < "$SCRIPT_DIR/$file"
done

echo "--- 4. Validate staged files ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- python3 -m py_compile $CT_DIR/app.py.new /home/git/hooks/build_image.py.new" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -n /home/git/hooks/post-receive.new" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -n /home/git/hooks/mirror-sync.sh.new" 2>&1

echo "--- 5. Back up and activate ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'set -e; stamp=\$(date -u +%Y%m%dT%H%M%SZ); [ ! -f $CT_DIR/app.py ] || cp -p $CT_DIR/app.py $CT_DIR/app.py.bak-\$stamp; [ ! -f /etc/init.d/git-http-server ] || cp -p /etc/init.d/git-http-server /etc/init.d/git-http-server.bak-\$stamp; for file in post-receive mirror-sync.sh build_image.py; do [ ! -f /home/git/hooks/\$file ] || cp -p /home/git/hooks/\$file /home/git/hooks/\$file.bak-\$stamp; done; mv $CT_DIR/app.py.new $CT_DIR/app.py; mv /etc/init.d/git-http-server.new /etc/init.d/git-http-server; for file in post-receive mirror-sync.sh build_image.py; do mv /home/git/hooks/\$file.new /home/git/hooks/\$file; done; chown git:git $CT_DIR/app.py /home/git/hooks/post-receive /home/git/hooks/mirror-sync.sh /home/git/hooks/build_image.py; chmod 0644 $CT_DIR/app.py; chmod 0755 /etc/init.d/git-http-server /home/git/hooks/post-receive /home/git/hooks/mirror-sync.sh /home/git/hooks/build_image.py; for repo in /home/git/repos/*.git; do [ -d \"\$repo\" ] || continue; mkdir -p \"\$repo/hooks\"; ln -sfn /home/git/hooks/post-receive \"\$repo/hooks/post-receive\"; done'" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-update add git-http-server default" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-service git-http-server restart" 2>&1

echo "--- 6. Health check ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'wget -qO- http://127.0.0.1:$GIT_HTTP_PORT/ >/dev/null && rc-service git-http-server status'" 2>&1

echo "== DONE =="
} 2>&1 | tee "$DEPLOY_LOG"
