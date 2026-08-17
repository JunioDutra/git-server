# ADR-0003: Automatic mirror/sync of repositories via git hooks

- **Status**: Proposed
- **Date**: 2026-08-17
- **Context**: Git server (LXC 109) + automatic mirror sync on push
- **Tags**: `adr`, `git-hooks`, `mirror`, `sync`, `repository.yaml`, `homelab`

---

## 1. Context

The git server (ADR-0001) hosts bare repos over SSH. Today, a repo
lives in exactly one place: `/home/git/repos/<name>.git`. When the
same code must exist elsewhere — a second server, a cloud provider
(GitHub/GitLab), a backup location — devs must push manually to each
destination, or a script must be maintained and run by hand.

We want: **whenever new commits are pushed to a repo that declares
mirror destinations, the server automatically pushes the same refs to
every configured mirror**.

The mirror list is declared **by the repo itself**, in the same
`repository.yaml` config introduced by ADR-0002 (D3). This keeps the
mirror policy next to the code, versioned, and per-repo — no
server-side whitelist to maintain.

### Forces

- **F-001**: Mirror sync must be triggered by git events (push), not
  by polling or manual commands.
- **F-002**: Both new repos (freshly created via the HTTP API) and
  existing repos (already on the server) must get the automation.
- **F-003**: The git server must stay lightweight — no extra
  long-running daemons for sync.
- **F-004**: Only repos that opt in (declare `mirrors` in
  `repository.yaml`) sync; others must be ignored (no noise, no
  accidental pushes to remote hosts).
- **F-005**: Sync must be **asynchronous and non-blocking**: a slow or
  unreachable mirror must not block the original push.
- **F-006**: Failures must be visible (logged per repo/mirror) without
  blocking the source push.
- **F-007**: Push feedback for the source repo stays fast and
  local-first — mirror latency is a background concern.

### Environment / assumptions

- Bare repos live under `/home/git/repos/<name>.git` (user `git`).
- HTTP creation service (`app.py`, ADR-0001/D3) runs as user `git`.
- `repository.yaml` at repo root already exists as the per-repo build
  config (ADR-0002-D3); this ADR **extends** it with a `mirrors` key.
- Central hook directory `/home/git/hooks/` and the installer
  `scripts/install-hooks.sh` from ADR-0002 (D2) are the canonical
  distribution mechanism for hook code.
- Mirror destinations may be SSH-URL git remotes
  (`git@host:path.git`, `ssh://git@host:port/path.git`) or HTTPS
  remotes with credentials.

---

## 2. Decisions

### ADR-0003-D1: Mirror config lives in `repository.yaml` under a `mirrors` key

- **Motivation**: the repo must declare *where* it should be mirrored
  to. Keeping it in `repository.yaml` (already introduced for
  builds) gives one config file per repo, versioned in the repo
  itself.
- **Decision**: extend `repository.yaml` with a `mirrors` key:
  ```yaml
  # repository.yaml
  dockerfile: Dockerfile            # existing build config (ADR-0002-D3)

  mirrors:                           # new: list of destinations
    - url: git@github.com:acme/foo.git
      # optional: only mirror specific branches; default: all pushed refs
      # branches: [main]
    - url: git@192.168.2.163:repos/foo-backup.git
  ```
  - `mirrors` is a **list**; a repo can have zero (default), one, or
    many mirrors.
  - Absent `mirrors` key → no sync (silent skip, same spirit as
    ADR-0002-D3).
  - `url` is required per entry; `branches` is optional (default:
    mirror every ref received by the hook).
  - URLs are validated: must start with `ssh://`, `git@`, `http://`,
    or `https://`; no local filesystem paths (`/home/...`, `./...`)
    to avoid pushing to the server itself.
- **Consequences**:
  - Single source of truth for repo behavior: build + mirror in one
    file.
  - Sync is opt-in — existing repos do nothing until they add the
    key.
  - The next pipeline task can reuse the same parsed config.
- **Alternatives**:
  - Server-side mirror registry (admin UI/API): rejected — adds a
    server-side state to maintain; repo-declared config keeps policy
    with the code.
  - Separate `mirrors.yaml`: rejected — splits repo config across
    files for no benefit.

### ADR-0003-D2: Mirror sync = extra `git push` from the same `post-receive` hook

- **Motivation**: the hook already fires on push with (oldrev, newrev,
  ref) on stdin — exactly the refs that must be mirrored.
- **Decision**:
  - The canonical `post-receive` hook (ADR-0002-D1) becomes a
    dispatcher: after the optional build step, it calls
    `scripts/mirror-sync.sh` with the pushed refs.
  - `mirror-sync.sh`:
    1. Reads `repository.yaml` from the pushed tree
       (`git archive <sha> | tar -x` — same materialization as the
       build, no worktree ops, per ADR-0001-D10).
    2. For each mirror entry, runs:
       `git push <mirror-url> <ref>` (or the refs matched by
       `branches`).
    3. Logs per mirror; failure in one mirror does not stop the
       others.
  - Sync runs **in background** (like the build, ADR-0002-D7) so the
    original push returns immediately.
- **Consequences**:
  - No new hook types, no client changes — the existing trigger
    covers both build and mirror.
  - Mirroring is eventually consistent: what was pushed is what gets
    mirrored (same refs, same SHAs).
- **Alternatives**:
  - `git config --add remote.<name>.pushurl` on each bare repo +
    `git push --mirror` in the hook: works but requires per-repo
    config mutation outside the repo tree; `repository.yaml` keeps it
    all visible and versioned.
  - Periodic cron sync: rejected — F-001 (event-driven), and mirrors
    drift between runs.

### ADR-0003-D3: Sync uses the pushed refs, not a full mirror push

- **Motivation**: `git push --mirror` sends *everything* (all refs,
  including stale ones) on every trigger; pushing only the refs that
  just changed is minimal and predictable.
- **Decision**:
  - For each ref line from the hook (`<old> <new> <ref>`), if
    `new` is all-zeros → deletion; push `--delete <ref>` to mirrors.
    Otherwise push `<new>:<ref>`.
  - Default: all refs. With `branches: [...]` in the entry, only
    refs whose branch name matches are mirrored.
  - Deletions propagate too — a branch deleted locally disappears on
    mirrors (matches the "same code everywhere" intent).
- **Consequences**:
  - Bandwidth and mirror churn stay proportional to actual changes.
  - Semantics are clear: mirrors replicate exactly what the server
    received.
- **Alternatives**:
  - `git push --mirror` each time: rejected — sends all refs
    (wasteful) and can resurrect refs deleted on the server.
  - rsync of objects: rejected — needs working tree/materialization
    on the mirror side; git push is the native protocol.

### ADR-0003-D4: Reuse the central hook + template + installer machinery (ADR-0002-D2)

- **Motivation**: F-002. Mirror sync must be present on new repos and
  retrofittable on existing ones — exactly the same distribution
  problem solved for build hooks.
- **Decision**:
  - The **same** canonical hook at `/home/git/hooks/post-receive`
    handles both build (ADR-0002) and mirror (this ADR). One file,
    one install.
  - New repos: `git init --bare --template=/home/git/hooks` keeps
    giving fresh repos the hook with both actions wired.
  - Existing repos: the idempotent `scripts/install-hooks.sh`
    symlinks the canonical hook into every bare repo — already
    covered, no new installer.
  - Templates (D2/D4 of ADR-0002) are updated so new repos come
    pre-wired; admins re-run the installer once for legacy repos.
- **Consequences**:
  - Single hook file = single place to update; both actions evolve
    together.
  - No repo needs a second install step; the machine from ADR-0002
    already handles it.
- **Alternatives**:
  - Separate `post-receive` for mirror vs build: rejected — hooks are
    single-file per repo; chaining them requires either concatenation
    or a dispatcher. Dispatcher wins (ADR-0002-D1 already owns the
    trigger).

### ADR-0003-D5: Async, per-mirror logging, never block the push

- **Motivation**: F-005/F-006/F-007. A mirror can be slow (cloud
  push), unreachable, or rejected; the original push must not wait.
- **Decision**:
  - The hook fires `mirror-sync.sh` in background, same pattern as
    the build (ADR-0002-D7).
  - Logs: `/home/git/logs/mirrors/<repo>.log` — one line per mirror
    attempt with result; failures append a `FAILED` block with exit
    code and stderr excerpt.
  - Each mirror is attempted independently; one failing mirror never
    blocks the others.
  - (Future: same Telegram/Home-Assistant webhook channel as build
    failures.)
- **Consequences**:
  - Pushes stay fast; mirror health is observable via logs.
  - Mirror failures are eventually noticed by whoever watches the
    log — or by the future notification hook.
- **Alternatives**:
  - Sync-blocking push: rejected — a dead mirror would freeze every
    push to the source repo.
  - Silent ignore on failure: rejected — hides drift between source
    and mirrors.

### ADR-0003-D6: Credentials for mirrors — per-mirror, out of the repo

- **Motivation**: mirror URLs may need credentials (HTTPS tokens,
  SSH keys). They must not be committed into `repository.yaml` (which
  lives inside the repo and is public to repo readers).
- **Decision**:
  - `repository.yaml` carries only the **destination URL** without
    secrets (`https://github.com/acme/foo.git`, `git@github.com:...`).
  - Secrets live server-side:
    - SSH: keys in `/home/git/.ssh/` (user `git`), with
      `~/.ssh/config` entries for mirror hosts if needed.
    - HTTPS: credentials stored in `/home/git/.git-credentials` or
      per-mirror `git config credential.helper` entries.
  - The hook pushes as user `git`; it never reads secrets from the
    repo tree.
- **Consequences**:
  - Repos stay shareable without leaking tokens.
  - One ops surface (`/home/git/.ssh`, credential helpers) to manage
    per mirror host.
- **Alternatives**:
  - Embed `user:token@` in the URL inside `repository.yaml`: rejected
    — secrets in the repo tree, visible to every reader, and
    re-synced to mirrors (mirror of secrets).
  - Server-side global credential store mapping URL→credential:
    rejected — same state problem as D1 alternative; keep it to the
    standard git mechanism.

---

## 3. Architecture overview

```
Client push ──> SSH ──> git@192.168.2.163 (LXC 109)
                          │  post-receive hook (dispatcher)
                          ├──▶ scripts/build-image.sh   (ADR-0002)
                          └──▶ scripts/mirror-sync.sh   (this ADR)
                                    │  git archive | tar → read repository.yaml
                                    │  mirrors[] present? ──no──▶ exit 0 (skip)
                                    │  yes
                                    ▼
                          for each mirror (background, independent):
                                    │  git push <mirror-url> <pushed-refs>
                                    ▼
                          /home/git/logs/mirrors/<repo>.log   (per-mirror result)

Mirror destinations: git@github.com:acme/foo.git
                     git@192.168.2.163:repos/foo-backup.git
                     https://github.com/acme/foo.git  (creds in /home/git/.git-credentials)
```

## 4. Files to be implemented (future work)

- `/home/git/hooks/post-receive` — canonical hook (dispatcher: build +
  mirror) — **already planned in ADR-0002; extended here**
- `/home/git/hooks/mirror-sync.sh` — read `repository.yaml` mirrors →
  push refs → log per mirror
- `scripts/install-hooks.sh` — idempotent installer (already planned
  in ADR-0002); re-run once to cover legacy repos with the new hook
- `/home/git/logs/mirrors/` — per-repo mirror logs (created on demand)
- (Ops) `/home/git/.ssh/` + `~/.ssh/config` + `.git-credentials` —
  per-mirror credentials, out of repo trees

## 5. Summary table

| # | Decision | Status |
|---|----------|--------|
| D1 | Mirror destinations declared in root `repository.yaml` (`mirrors` list; opt-in) | Proposed |
| D2 | Sync runs from the same `post-receive` hook via `mirror-sync.sh` | Proposed |
| D3 | Push only the refs received (deletions included), not `--mirror` | Proposed |
| D4 | Reuse central hook + template + installer (ADR-0002-D2); new repos pre-wired, legacy via installer | Proposed |
| D5 | Async sync, per-mirror logs, never block the push | Proposed |
| D6 | Mirror credentials server-side (SSH keys / credential helper), never in `repository.yaml` | Proposed |

---

*Generated 2026-08-17. Decision record — implementation pending
subsequent task.*
