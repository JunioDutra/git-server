# ADR-0002: Automated container builds via git hooks (server-side)

- **Status**: Proposed
- **Date**: 2026-08-17
- **Context**: Git server (LXC 109) + automatic Docker builds on push
- **Tags**: `adr`, `git-hooks`, `docker`, `buildx`, `registry`, `homelab`

---

## 1. Context

The git server (ADR-0001) hosts bare repos over SSH. Today, when a repo
gains a `Dockerfile`, building the image is a **manual** step: devs must
pull, build, tag and push somewhere — or run a pipeline elsewhere.

We want: **whenever new commits are pushed to a repo that contains a
`Dockerfile` in its root** (new repos or existing repos), the server
should automatically:

1. Detect the `Dockerfile`.
2. Build the image with `docker buildx`.
3. Push the image to a registry.
4. Clean up all temporary artifacts afterward.

The build must **not execute on the git server itself**. The plan is to
connect `buildx` to a **remote Docker daemon** (a dedicated build node),
keeping the git LXC free from Docker workloads and able to stay tiny.

### Forces

- **F-001**: Builds must be triggered by git events (push), not by
  polling or manual commands.
- **F-002**: Both new repos (freshly created via the HTTP API) and
  existing repos (already on the server) must get the automation.
- **F-003**: The git server must stay lightweight — no local Docker
  daemon, no local build cache, no local registry storage.
- **F-004**: Only repos with a `Dockerfile` at the root build; others
  must be ignored (no noise).
- **F-005**: Every build attempt must produce a tagged image pushable
  to a registry; builds must be reproducible from a commit SHA.
- **F-006**: Cleanup must be complete — no leftover temp dirs, no
  stray build contexts, no orphan images/cache on the git server.

### Environment / assumptions

- Bare repos live under `/home/git/repos/<name>.git` (user `git`).
- HTTP creation service (`app.py`, ADR-0001/D3) runs as user `git`.
- A registry is available on the LAN (see D6 for the chosen default).
- A build node with a Docker daemon is reachable from the git server
  (DOCKER_HOST / docker context / buildx driver).

---

## 2. Decisions

### ADR-0002-D1: Trigger = git `post-receive` hook

- **Motivation**: the hook fires exactly when the thing that should
  trigger a build happens — a successful push into a bare repo. It
  receives (oldrev, newrev, ref) per ref on stdin, giving the build
  script the exact commits to process.
- **Decision**: every bare repo gets a `post-receive` hook that:
  1. Reads the pushed refs (typically `refs/heads/*`), resolves the
     new tree.
  2. Runs the build script if a `Dockerfile` exists at the repo root.
- **Consequences**:
  - Real-time, no polling, no external scheduler.
  - Hook lifecycle is tied to the repo — every repo must be
    INSTALLED with the hook (D2).
- **Alternatives**:
  - Cron scanning repos: rejected — 15..60s latency, races with the
    current push, extra statefulness.
  - Webhook from client: rejected — requires client-side tooling and
    trust.

### ADR-0002-D2: Hook distribution — central hook dir + auto-install

- **Motivation**: hooks are per-repo files
  (`<repo>.git/hooks/post-receive`). Managing N copies by hand is
  error-prone.
- **Decision**:
  - Store ONE canonical hook script at `/home/git/hooks/post-receive`
    (versioned in the `git-server` repo).
  - New repos: `git init --bare --template=/home/git/hooks` so every
    fresh repo gets the hook from creation.
  - Existing repos: `scripts/install_hooks.sh` symlinks
    `/home/git/hooks/post-receive` into
    `/home/git/repos/<name>.git/hooks/post-receive` for every bare
    repo. Idempotent, re-runnable.
- **Consequences**:
  - Updating the hook = replacing one file + re-running installer.
  - Repos created before the change are covered by the installer
    script.
- **Alternatives**:
  - Symlink farm maintained by `app.py` at creation: rejected — HTTP
    service should stay creation-focused; the template approach
    covers it for new repos anyway, installer covers legacy.
  - `core.hooksPath` global: `core.hooksPath` can't be set per-all
    repos by the bare repo itself; it needs a config step per repo —
    rejected for the same automation reason.

### ADR-0002-D3: Dockerfile detection — root only, via `git archive`

- **Motivation**: building arbitrary commits requires materializing
  the tree without touching the bare repo working state (ADR-0001
  D10 lesson).
- **Decision**:
  - The hook exports the pushed tree with `git archive <sha> |
    tar -x -C "$workdir"`.
  - Detection: `[ -f "$workdir/Dockerfile" ]` — root only. Sub-tree
    Dockerfiles do not trigger builds (documented; can be relaxed
    later with an explicit opt-in list).
  - The workdir is a `mktemp -d` per build; ownership `git`.
- **Consequences**:
  - No worktree ops on bare repos — safe per ADR-0001/D10.
  - Archive shows the tree exactly as committed; no uncommitted
    surprises.
- **Alternatives**:
  - `git ls-tree -r --name-only HEAD | grep -E '(^|/)Dockerfile'`
    and still archive: works, but the tar step is needed anyway for
    the build context.
  - `git worktree add` on the bare: rejected — this EXACTLY
    recreates the ADR-0001/D10 foot-gun.

### ADR-0002-D4: Build engine — `docker buildx` connected to a remote
- **Motivation**: (F-003) the git server runs Alpine with no Docker
  daemon and must stay that way; builds need a real engine with
  cache, context transfer, and multi-platform support.
- **Decision**: `docker buildx` with a **remote driver / remote
  DOCKER_HOST** pointing at the dedicated build node:
  - Example: `DOCKER_HOST=tcp://build-node:2375`
    `docker buildx build --push -t <registry>/<repo>:<sha> .`
  - Buildx keeps the client thin; all layers, cache, and execution
    occur on the build node.
  - The git server only needs the `docker`/`buildx` **CLI** (from
    `apk`/static binary), not a daemon.
- **Consequences**:
  - git LXC stays clean; the heavy lifting happens on a node the
    user can scale.
  - TLS/authentication for the remote daemon becomes a security
    requirement (ADRD-0002-D8).
- **Alternatives**:
  - Buildkit daemon inside the git LXC: rejected — defeats F-003;
    the container hosts git only, and adds RAM/disk usage.
  - Jenkins as CI: rejected — heavyweight for a LAN; the hook +
    buildx covers the need with 3 scripts.

### ADR-0002-D5: Registry — local registry service, SHA + latest tags

- **Motivation**: built images need a canonical destination for
  deploy/ rollback; tags must encode the source commit.
- **Decision**:
  - Stand up a local `registry:2` Docker service (e.g. on the build
    node or a small LXC) listening on `192.168.2.163:5000` (LAN
    TCP).
  - Build tags:
    - `192.168.2.163:5000/<repo>:<short-sha>` — immutable, per
      commit.
    - `192.168.2.163:5000/<repo>:latest` — only for pushes to the
      default branch (`refs/heads/main`).
- **Consequences**:
  - Simple, predictable IaC — images are reachable from the LAN
    without cloud creds.
  - Registry service with no users/ops; runs as a single container
    with storage on the node.
- **Alternatives**:
  - GHCR/DockerHub: rejected — external, needs cloud tokens, and
    the homelab already has internal infra.
  - Harbor: overkill for LAN scale.

### ADR-0002-D6: Cleanup — every step leaves nothing behind

- **Motivation**: repeated builds on a small server accumulate
  context dirs and old images; the server must stay clean.
- **Decision** (post-build, always, in `trap`):
  - `rm -rf` the `mktemp` workdir.
  - `docker container prune -f`, `docker image prune -af`
    `docker builder prune -af --filter until=24h`
    on the REMOTE daemon (stateless cache only 24h).
  - The git server itself never accumulates build context (only the
    temporary tar extraction dir).
- **Consequences**:
  - No stuck contexts; low disk-blow concern.
  - Pruning is done via the same remote CLI so the build node's
    long-term image cache stays bounded (24h).
- **Alternatives**:
  - Persistent workspace per repo: rejected — undo of the
    reproducibility and cache-read at the git level.

### ADR-0002-D7: Fail loudly, report, never block the push

- **Motivation**: a broken Dockerfile should NOT roll back the git
  push itself (push is source-of-truth; build feedback moves to a
  log/channel).
- **Decision**:
  - The post-receive hook runs the build **in background**
    (`npm -f >/dev/null 2>&1 &`) and returns success immediately to
    the pusher.
  - Logs per repo: `/home/git/logs/builds/<repo>.log` (rotated).
  - Build failures append a `FAILED` block with the exit code.
- **Consequences**:
  - Pushes stay fast even when builds are slow.
  - Devs discover failures by reading the log/notification (future:
    Telegram/Home-Assistant webhook).
- **Alternatives**:
  - Hook ignores errors (silent): rejected — hides everything.
  - Sync build blocking the push: rejected — pushes become
    dependent on infra that may be down.

---

## 3. Architecture overview

```
Client push ──> SSH ──> git@192.168.2.163 (LXC 109)
                          │  post-receive hook (per repo)
                          ▼
              scripts/build-image.sh
                  │  git archive | tar → $workdir
                  │  [ -f Dockerfile ]  ──no──▶ exit 0 (skip)
                  │  yes
                  ▼
        docker buildx ──remote DOCKER_HOST──▶ Build node (docker daemon)
                  │                              │ build images
                  ▼                              ▼
            registry:5000/<repo>:<sha>   (push, cache bounded 24h)
                  │
                  ▼
              cleanup (trap): rm -rf $workdir; prune remote
```

## 4. Files to be implemented (future work)

- `/home/git/hooks/post-receive` — canonical hook script
- `/home/git/hooks/build-image.sh` — archive→build→tag→push→prune
- `scripts/install-hooks.sh` — install hook into every current/repo
  bare repo; used by deploy for legacy repos
- `scripts/build-node-setup.sh` — provision Docker engine +
  registry on the build node (TLS option)

## 5. Summary table

| # | Decision | Status |
|---|----------|--------|
| D1 | post-receive git hook as trigger | Proposed |
| D1 | Central hook + template for new repos + installer for legacy | Proposed |
| D3 | Root Dockerfile detection via `git archive` (no worktree ops) | Proposed |
| D4 | docker buildx with remote driver → build node | Proposed |
| D5 | Local registry + `<sha>` and `:latest` tags | Proposed |
| D6 | Cleanup: rm workdir + remote prune (24h cache) | Proposed |
| D7 | Async build, logs, don't block push | Proposed |

---

*Generated 2026-08-17. Decision record — implementation pending
subsequent task.*