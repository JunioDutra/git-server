#!/bin/sh
# mirror-sync.sh — ADR-0003 D2/D3/D5: mirror pushed refs to configured
# destinations declared in repository.yaml.
#
# Invoked by the post-receive hook (asynchronously). Reads:
#   - $1 = repo bare dir   (/home/git/repos/<name>.git)
#   - $2 = repo name       (e.g. myrepo)
#   - remaining args: "<newrev>:<refname>" pairs from the hook stdin
#
# Behavior:
#   - If repository.yaml missing or has no 'mirrors' key -> silent skip.
#   - Reads 'mirrors' list: each entry has 'url' (required) and optional
#     'branches' (default: mirror every pushed ref).
#   - Pushes only the refs the hook received (deletions propagate as
#     --delete), never a full --mirror push (D3).
#   - Runs asynchronously (background) so the source push never blocks (D5).
#   - Logs per mirror to /home/git/logs/mirrors/<repo>.log (D5).
#   - One failing mirror never stops the others (D5).
#
# Deps: git, awk, tar, timeout (busybox)
set -u

# The hook may run as root (sshd session user) while the git server's
# mirror credentials live in /home/git/.ssh (user git). Force HOME and
# pin the identity + known_hosts explicitly so ssh uses the git user's
# keys regardless of the uid running the hook.
export HOME="${GIT_USER_HOME:-/home/git}"
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i /home/git/.ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=/home/git/.ssh/known_hosts -o StrictHostKeyChecking=yes}"

REPO_BARE="$1"
REPO_NAME="$2"
shift 2

LOG_DIR="/home/git/logs/mirrors"
WORK=""

log() { printf '%s\n' "$*"; }

fail() {
  echo "==== MIRROR FAILED ($(date -Is)) ====" >> "$LOG_DIR/$REPO_NAME.log"
  echo "exit=$1 reason=$2" >> "$LOG_DIR/$REPO_NAME.log"
  echo "==== END FAILED ====" >> "$LOG_DIR/$REPO_NAME.log"
  exit "$1"
}

cleanup() {
  [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$LOG_DIR" || true

echo "==== MIRROR START ($(date -Is)) repo=$REPO_NAME ====" >> "$LOG_DIR/$REPO_NAME.log"

# Pick a SHA to materialize the pushed tree: the last non-deletion newrev.
SHA=""
for pair in "$@"; do
  new=${pair%%:*}
  case "$new" in
    0000000000000000000000000000000000000000) continue ;;
  esac
  SHA="$new"
done
# Only deletions pushed -> use HEAD for config lookup (may still carry deletions).
if [ -z "$SHA" ]; then
  SHA=$(git --git-dir="$REPO_BARE" rev-parse HEAD 2>/dev/null || true)
fi
if [ -z "$SHA" ]; then
  echo "skip: no refs and no HEAD" >> "$LOG_DIR/$REPO_NAME.log"
  exit 0
fi

# Materialize the pushed tree (no worktree ops)
WORK=$(mktemp -d /tmp/mirror-XXXXXX) || fail 2 "mktemp"
git --git-dir="$REPO_BARE" archive "$SHA" | tar -x -C "$WORK" || fail 3 "git archive"

# Read repository.yaml
CFG="$WORK/repository.yaml"
if [ ! -f "$CFG" ]; then
  echo "skip: no repository.yaml (opt-in not set)" >> "$LOG_DIR/$REPO_NAME.log"
  exit 0
fi

# Parse mirrors: emit one line per entry: "<url>\t<branches-csv>" (branches may be empty)
MIRRORS=$(awk '
  /^[[:space:]]*mirrors[[:space:]]*:/ { in_mirrors=1; next }
  in_mirrors && /^[[:space:]]*-[[:space:]]*url[[:space:]]*:/ {
    if (cur_url != "") print cur_url "\t" branches
    u=$0; sub(/^[[:space:]]*-[[:space:]]*url[[:space:]]*:[[:space:]]*/, "", u); sub(/[[:space:]]+$/, "", u)
    cur_url=u; branches=""
    next
  }
  in_mirrors && /^[[:space:]]*branches[[:space:]]*:/ {
    line=$0
    if (line ~ /\[/) {
      sub(/^[^[]*\[/, "", line); sub(/\].*$/, "", line)
      gsub(/[[:space:]]/, "", line)
      branches=line
    }
    next
  }
  in_mirrors && /^[[:space:]]*-[[:space:]]/ {
    item=$0; sub(/^[[:space:]]*-[[:space:]]*/, "", item); sub(/[[:space:]]+$/, "", item)
    branches = (branches == "" ? item : branches "," item)
    next
  }
  END { if (cur_url != "") print cur_url "\t" branches }
' "$CFG")

if [ -z "$MIRRORS" ]; then
  echo "skip: repository.yaml has no 'mirrors' key" >> "$LOG_DIR/$REPO_NAME.log"
  exit 0
fi

# Push to each mirror independently
printf '%s\n' "$MIRRORS" | while IFS="$(printf '\t')" read -r url branches; do
  [ -z "$url" ] && continue

  # Validate URL (ADR-0003-D1): ssh/https/git@ only, no local paths
  case "$url" in
    ssh://*|git@*|http://*|https://*) ;;
    *)
      echo "skip: invalid mirror URL: $url" >> "$LOG_DIR/$REPO_NAME.log"
      continue
      ;;
  esac

  echo "mirror: $url branches=${branches:-all}" >> "$LOG_DIR/$REPO_NAME.log"
  err=0

  for pair in "$@"; do
    new=${pair%%:*}
    ref=${pair#*:}

    # Branch filter (optional)
    if [ -n "$branches" ]; then
      case "$ref" in
        refs/heads/*) b=${ref#refs/heads/} ;;
        *) continue ;;
      esac
      matched=no
      IFS=','
      for bb in $branches; do
        [ "$bb" = "$b" ] && matched=yes
      done
      unset IFS
      [ "$matched" = no ] && continue
    fi

    case "$new" in
      0000000000000000000000000000000000000000)
        if ! timeout 60 git push "$url" --delete "$ref" >> "$LOG_DIR/$REPO_NAME.log" 2>&1; then
          err=1
        fi
        ;;
      *)
        if ! timeout 60 git push "$url" "$new:$ref" >> "$LOG_DIR/$REPO_NAME.log" 2>&1; then
          err=1
        fi
        ;;
    esac
  done

  if [ "$err" -eq 0 ]; then
    echo "mirror OK: $url" >> "$LOG_DIR/$REPO_NAME.log"
  else
    echo "==== MIRROR FAILED ($(date -Is)) mirror=$url ====" >> "$LOG_DIR/$REPO_NAME.log"
    echo "==== END FAILED ====" >> "$LOG_DIR/$REPO_NAME.log"
  fi
done

echo "==== MIRROR END ($(date -Is)) ====" >> "$LOG_DIR/$REPO_NAME.log"
exit 0
