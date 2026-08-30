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
NON_SECRET_ENV_NAMES="GIT_HTTP_HOST GIT_HTTP_PORT GIT_SSH_HOST GIT_DEFAULT_BRANCH GIT_REPOS_ROOT GIT_HOOKS_ROOT GIT_HOOK_LOGS_ROOT GIT_HOOK_LOG_RETENTION_DAYS GIT_BUILD_SOCKET GIT_REPOSITORY_ENV_ROOT"

{
echo "=== Deploy git-http-server to container $GIT_CONTAINER_ID ==="

echo "--- 1. Validate inherited container environment ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'missing=; for name in GIT_HTTP_HOST GIT_HTTP_PORT GIT_SSH_HOST GIT_DEFAULT_BRANCH REGISTRY_ADDRESS REGISTRY_USER REGISTRY_PASSWORD REGISTRY_INSECURE BUILDKIT_ADDRESS BUILDX_BUILDER; do value=\$(printenv \"\$name\"); [ -n \"\$value\" ] || missing=\"\$missing \$name\"; done; [ -z \"\$missing\" ] || { echo \"Missing required container environment variables:\$missing\" >&2; exit 2; }'" 2>&1

echo "--- 2. Configure the isolated OpenRC environment model ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'set -e; marker=\"# git-server non-secret environment\"; expected=\"rc_env_allow=\\\"\\\${rc_env_allow} $NON_SECRET_ENV_NAMES\\\"\"; tmp=\$(mktemp); awk '\''\$0 == \"# git-server runtime environment\" || \$0 == \"# git-server frontend environment\" || \$0 == \"# git-server non-secret environment\" { skip=1; next } skip { skip=0; next } /^rc_env_allow=.*REGISTRY_ADDRESS.*REGISTRY_PASSWORD.*BUILDKIT_ADDRESS/ { next } { out[++n]=\$0 } END { while (n && out[n] == \"\") n--; for (i=1; i<=n; i++) print out[i] }'\'' /etc/rc.conf > \"\$tmp\"; printf \"\\n%s\\n%s\\n\" \"\$marker\" \"\$expected\" >> \"\$tmp\"; if ! cmp -s /etc/rc.conf \"\$tmp\"; then stamp=\$(date -u +%Y%m%dT%H%M%SZ); cp -p /etc/rc.conf /etc/rc.conf.bak-\$stamp; cat \"\$tmp\" > /etc/rc.conf; fi; rm -f \"\$tmp\"'" 2>&1

echo "--- 3. Install runtime dependencies and directories ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- apk add --no-cache python3 py3-yaml" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'mkdir -p $CT_DIR /home/git/hooks /home/git/logs/hooks \"\${GIT_REPOSITORY_ENV_ROOT:-/home/git/repository-env}\" && chown -R git:git $CT_DIR /home/git/hooks /home/git/logs \"\${GIT_REPOSITORY_ENV_ROOT:-/home/git/repository-env}\" && chmod 0700 \"\${GIT_REPOSITORY_ENV_ROOT:-/home/git/repository-env}\"'" 2>&1

echo "--- 4. Stage application, services, and hooks ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > $CT_DIR/app.py.new'" < "$SCRIPT_DIR/app.py"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > $CT_DIR/repository_variables.py.new'" < "$SCRIPT_DIR/repository_variables.py"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > $CT_DIR/configure_build_env.py.new'" < "$SCRIPT_DIR/configure_build_env.py"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > /etc/init.d/git-http-server.new'" < "$SCRIPT_DIR/git-http-server.initd"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > /etc/init.d/git-build-dispatcher.new'" < "$SCRIPT_DIR/git-build-dispatcher.initd"
for file in post-receive mirror-sync.sh build_image.py build_submit.py build_dispatcher.py repository_variables.py; do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
    "pct exec $GIT_CONTAINER_ID -- sh -c 'cat > /home/git/hooks/$file.new'" < "$SCRIPT_DIR/$file"
done

echo "--- 5. Validate staged files ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- python3 -m py_compile $CT_DIR/app.py.new $CT_DIR/repository_variables.py.new $CT_DIR/configure_build_env.py.new /home/git/hooks/build_image.py.new /home/git/hooks/build_submit.py.new /home/git/hooks/build_dispatcher.py.new /home/git/hooks/repository_variables.py.new" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -n /home/git/hooks/post-receive.new /home/git/hooks/mirror-sync.sh.new /etc/init.d/git-http-server.new /etc/init.d/git-build-dispatcher.new" 2>&1

echo "--- 6. Back up and activate ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'set -e; stamp=\$(date -u +%Y%m%dT%H%M%SZ); for file in app.py repository_variables.py configure_build_env.py; do [ ! -f $CT_DIR/\$file ] || cp -p $CT_DIR/\$file $CT_DIR/\$file.bak-\$stamp; done; for service in git-http-server git-build-dispatcher; do [ ! -f /etc/init.d/\$service ] || cp -p /etc/init.d/\$service /etc/init.d/\$service.bak-\$stamp; done; for file in post-receive mirror-sync.sh build_image.py build_submit.py build_dispatcher.py repository_variables.py; do [ ! -f /home/git/hooks/\$file ] || cp -p /home/git/hooks/\$file /home/git/hooks/\$file.bak-\$stamp; done; mv $CT_DIR/app.py.new $CT_DIR/app.py; mv $CT_DIR/repository_variables.py.new $CT_DIR/repository_variables.py; mv $CT_DIR/configure_build_env.py.new $CT_DIR/configure_build_env.py; mv /etc/init.d/git-http-server.new /etc/init.d/git-http-server; mv /etc/init.d/git-build-dispatcher.new /etc/init.d/git-build-dispatcher; for file in post-receive mirror-sync.sh build_image.py build_submit.py build_dispatcher.py repository_variables.py; do mv /home/git/hooks/\$file.new /home/git/hooks/\$file; done; chown git:git $CT_DIR/app.py $CT_DIR/repository_variables.py /home/git/hooks/post-receive /home/git/hooks/mirror-sync.sh /home/git/hooks/build_image.py /home/git/hooks/build_submit.py /home/git/hooks/build_dispatcher.py /home/git/hooks/repository_variables.py; chown root:root $CT_DIR/configure_build_env.py /etc/init.d/git-http-server /etc/init.d/git-build-dispatcher; chmod 0644 $CT_DIR/app.py $CT_DIR/repository_variables.py /home/git/hooks/repository_variables.py; chmod 0750 $CT_DIR/configure_build_env.py; chmod 0755 /etc/init.d/git-http-server /etc/init.d/git-build-dispatcher /home/git/hooks/post-receive /home/git/hooks/mirror-sync.sh /home/git/hooks/build_image.py /home/git/hooks/build_submit.py /home/git/hooks/build_dispatcher.py; for repo in /home/git/repos/*.git; do [ -d \"\$repo\" ] || continue; mkdir -p \"\$repo/hooks\"; ln -sfn /home/git/hooks/post-receive \"\$repo/hooks/post-receive\"; done'" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- $CT_DIR/configure_build_env.py" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-update add git-build-dispatcher default" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-update add git-http-server default" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-service git-build-dispatcher restart" 2>&1
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- rc-service git-http-server restart" 2>&1

echo "--- 7. Health check ---"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$PROX_TARGET" \
  "pct exec $GIT_CONTAINER_ID -- sh -c 'healthy=; attempt=1; while [ \"\$attempt\" -le 15 ]; do if wget -qO- http://127.0.0.1:$GIT_HTTP_PORT/ >/dev/null && [ -S \"\${GIT_BUILD_SOCKET:-/run/git-server/build.sock}\" ]; then healthy=1; break; fi; sleep 1; attempt=\$((attempt + 1)); done; [ \"\$healthy\" = 1 ] || { rc-service git-build-dispatcher status >&2; tail -50 /home/git/logs/build-dispatcher.log >&2; rc-service git-http-server status >&2; tail -50 $CT_DIR/server.log >&2; exit 1; }; rc-service git-build-dispatcher status; rc-service git-http-server status'" 2>&1

echo "== DONE =="
} 2>&1 | tee "$DEPLOY_LOG"
