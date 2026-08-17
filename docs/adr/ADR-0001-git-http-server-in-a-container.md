# ADR-0001: git-http-server — In an LXC Container

- **Status**: Accepted
- **Date**: 2026-08-17
- **Context**: Homelab git server + HTTP management service
- **Tags**: `adr`, `git`, `lxc`, `python`, `openrc`, `homelab`

---

## 1. Context

The homelab needs a self-hosted git server that (a) hosts bare
repositories reachable over SSH, and (b) provides an HTTP service for
creating, browsing, and deleting those repositories from a web
browser. The environment is a Proxmox host (`192.168.2.150`) already
running LXC containers; an available IP was `192.168.2.163`.

This ADR records the full decision history of the service, from
container creation through the current verified state. It lives in the
service's own git repository (`git-server`, self-hosted on the same
server) as durable documentation.

### Forces

- **F-001**: The service must be isolated from the Proxmox host and
  from other containers (security, resource limits, clean lifecycle).
- **F-002**: Git over SSH is the primary transport; SSH push/pull from
  client machines must work against bare repos.
- **F-003**: The HTTP service must be simple to deploy, audit, and
  maintain; dependency bloat is a cost.
- **F-004**: The service must survive container reboots without manual
  intervention.
- **F-005**: The HTTP surface accepts repository-creation input;
  input validation and privilege separation are mandatory.
- **F-006**: Feature evolution (browse UI, create form, delete) must
  not break existing SSH push behavior.
- **F-007**: The service should host itself (dogfooding) to prove the
  workflow end-to-end.

### Resulting environment (facts)

- LXC container ID `109` on Proxmox host `192.168.2.150`.
- OS: Alpine Linux 3.23.5; IP: `192.168.2.163`.
- Bare repos under `/home/git/repos/<name>.git`, owned by system user
  `git`; accessed via `git@192.168.2.163:repos/<name>.git`.
- HTTP service: stdlib-only Python 3 (`app.py`) on port `8080`, run
  by user `git`.
- Service source lives in the `git-server` repo on the same server,
  created through the API itself.

---

## 2. Decision Record

### D1 — Host the service in an LXC container with Alpine Linux

**Motivation**: Proxmox is the hosting platform; LXC is its native,
lightweight isolation layer. Alpine provides a tiny, fast-boot,
low-footprint userspace base. The service only needs Python 3 (+ curl
for tests), both installed via `apk` — no other packages.

**Decision**: provision LXC container ID `109` on `192.168.2.150`,
Alpine Linux 3.23.5, IP `192.168.2.163`. The container is dedicated to
the git server.

**Consequences**:
- Minimal overhead vs a full VM; near-host performance on git
  filesystem operations (SSH).
- Clean isolation: git processes and repo data cannot affect the
  host hypervisor.
- Easy lifecycle: backup, delete, re-create independently of the host.
- One more network service to patch and monitor.

**Alternatives**:
- Full VM: rejected — heavier (I/O, memory, management) for a small
  git service.
- Install directly on the Proxmox host: rejected — violates
  isolation and expands host trust surface.

**References**: `30ffcc8` (initial commit).

---

### D2 — SSH as the primary git transport, with bare repos

- **Motivation**: git-over-SSH is the native, battle-tested transport;
  hook support and access control via SSH keys work out of the box.
- **Decision**: all repos are bare repos under
  `/home/git/repos/<name>.git`, owned by system user `git`. Clients
  clone via `git@192.168.2.163:repos/<name>.git`. The HTTP service
  only creates/deletes/browses; it never proxies git traffic.
- **Pros**:
  - Standard server model; `git clone`/`push`/`pull` work with zero
    custom tooling.
  - Hooks, ref policies, and key-based access all available.
- **Cons**:
  - Repo CRUD still needs a client or the HTTP UI; plain SSH has no
    web browse story (addressed by D4).
  - Each client needs an SSH key set up once.
- **Alternatives**:
  - Smart-HTTP git protocol (`git-http-backend`): rejected — extra
    auth/proxy complexity not needed for the homelab scale.
- **References**: `30ffcc8`.

---

### D3 — Zero-dependency Python HTTP service (stdlib only)

- **Motivation**: The management surface is small (create, browse,
  view, delete, log). A full framework brings a dependency tree
  for no benefit at this size. One auditable file wins over framework
  boilerplate.
- **Decision**: `app.py` is a Python 3 stdlib-only HTTP server
  (`http.server`), listening on port `8080`, with endpoints:
  - `POST /create` — body `{"name":"my-repo"}` (JSON or form);
    creates bare repo with `git init --bare -b main`; returns
    `201` / `400` / `409`.
  - `GET /` — HTML index listing all repos + create-form.
  - `GET /repo/<name>/` — repo tree at HEAD.
  - `GET /repo/<name>/tree/<path>` — browse subdirectory.
  - `GET /repo/<name>/blob/<path>` — view file content (escaped).
  - `GET /repo/<name>/log` — recent commits.
  - `DELETE /repo/<name>` — delete repo (404 if missing).
- **Pros**:
  - No `pip` packages; no supply-chain surface; `requirements.txt`
    intentionally holds a comment only.
  - Whole app fits in one file — easy to diff and review.
- **Cons**:
  - Not a framework: no authn, no rate limiting, no automatic
    routing. Manual JSON parse and path handling required.
- **Alternatives**:
  - Flask/FastAPI: rejected — external deps for no gain at this size.
  - Node.js/Go: rejected — heavier toolchain than needed; JS only
    appears client-side (D4).
- **References**: `30ffcc8`, `cadf414`.

---

### D4 — Web UI with client-side rendering via CDN

- **Motivation**: The repo page should render README.md nicely and
  offer a usable web interface, without adding a server-side markdown
  dependency.
- **Decision**: the UI fetches the repo-root `README.md` (fallback
  names: `readme.md`, `README.MD`, `Readme.md`, `README.markdown`,
  `README.txt`, `README`) and renders it client-side with
  `marked@12.0.2` + `DOMPurify@3.1.6` loaded from jsDelivr CDN.
  Raw markdown is HTML-escaped into a hidden `<pre>` and rendered
  into the page; relative links are rewritten to
  `/repo/<name>/blob/<path>`, external links pass through.
- **Pros**:
  - Server stays stdlib-only; zero server-side JS/markdown payload.
  - README rendering consistent with modern browsers.
- **Cons**:
  - CDN dependency at page load; without internet the README stays
    hidden (raw only in a hidden pre; acceptable trade-off).
- **Alternatives**:
  - Server-side markdown engine (e.g. `markdown` pip package):
    rejected — adds a server dep.
- **References**: `2ab32fe`.

---

### D5 — Create-repo form and Delete button in the UI

- **Motivation**: The API alone forces curl/JSON workflows; a plain
  web UI needed for everyday use. Kept minimal — no JS framework.
- **Decision**:
  - Index page gets an input + "Create" button. On submit: native
    `confirm()` dialog, then `fetch POST /create`; success → green
    message + redirect to the new repo page after ~800 ms; error →
    red message. Same regex validation as the API.
  - Repo page gets a red "Delete repository" button. Native
    `confirm()` with "cannot be undone" warning, then
    `DELETE /repo/<name>`; success → redirect to index. Works for
    empty repos too.
- **Pros**:
  - No framework (Vue/React would be overkill for 5 endpoints).
  - Destructive action guarded by a deliberate confirm.
- **Cons**:
  - Delete is permanent by design (only recoverable from backup);
    acceptable and stated in the UI.
- **Alternatives**:
  - Framework-based SPA: rejected — overkill.
- **References**: `60b3ed4`, `5908900`, `2734307`.

---

### D6 — Bare repos default branch `main`, client-side workaround

- **Motivation**: The modern default branch is `main`; create repos
  with it from the start.
- **Decision**: The server runs `git init --bare -b main`.
- **Known behavior**: Cloning an *empty* repo defaults the local
  checkout branch to `master` (git client default), so the server's
  `main` doesn't propagate. Documented workaround: client runs
  `git branch -m main` before the first push.
- **Alternatives**:
  - Keep `master` default: rejected — stale convention.
  - Auto-rename on server side: not possible cleanly for a bare and
    empty repo without knowledge of the clone; documented instead.
- **References**: `30ffcc8` (creation), `cadf414` (README).

---

### D7 — OpenRC as service manager (reboot-safe)

- **Motivation**: Initial launches used `setsid` + `nohup` — survives
  SSH disconnect but not container reboot. A later `start.sh` used
  `pkill -f app.py`, which could SIGTERM the SSH session itself
  (exit 143) because the pattern matched the shell command line.
- **Decision**: Added `/etc/init.d/git-http-server` (OpenRC init
  script, runs as user `git`), registered in the `default` runlevel.
  `start.sh` now wraps `rc-service git-http-server stop|start`.
  Verified with a real container reboot: server process running,
  external endpoint returned `200`, `rc-service status` → `started`.
- **Pros**:
  - Starts automatically after container reboot; no manual step.
  - Matches Alpine's native service model (no systemd otherwise).
  - Removes the `pkill` foot-gun and the setsid/nohup dance.
- **Cons**:
  - Requires the initd file to live alongside the code (versioned in
    repo) and be copied during deploy.
- **Alternatives**:
  - systemd unit: rejected — Alpine ships OpenRC.
  - Cron-based restart / watchdog: rejected — fragile.
- **References**: `c726969`, `a1e5e64`.

---

### D8 — Security: run as `git`, not root; regex + safe_subpath

- **Motivation**:
  - Reliability: if the server runs as root, created repos are
    `root:root`, and SSH push fails with "dubious ownership" errors.
  - Security: the HTTP surface accepts arbitrary names and paths;
    input validation is required (least privilege on a LAN service).
- **Decision**:
  1. The HTTP server always runs as system user `git` (not root).
  2. Repo name validated by regex: `[A-Za-z0-9._-]+`; anything else
     → `400`.
  3. `safe_subpath()` rejects any `..` (path traversal) → `400`;
     verified with `curl --path-as-is`.
  4. Delete uses `shutil.rmtree` only on a validated repo path.
- **Pros**:
  - Created repos are `git:git` → SSH push works.
  - Traversal cannot escape `/home/git/repos`.
  - Server process has no superuser powers.
- **Cons**:
  - Discipline required: must never relaunch as root (the repo README
    notes this as critical).
- **Alternatives**:
  - Run as a dedicated non-`git` service user: considered; not needed
    since `git` already exists as a system user with the right
    ownership model.
- **References**: `30ffcc8` (initial), `2734307` (safe_subpath
  implementation).

---

### D9 — Self-versioning / dogfooding

- **Motivation**: The service should host itself — the strongest
  end-to-end test of the API and its docs.
- **Decision**: The service source lives in the `git-server` repo on
  the same server, created through `POST /create` (dogfooding the
  API). Files: `app.py`, `README.md`, `deploy.sh`, `start.sh`,
  `check.sh`, `flow.sh`, `git-http-server.initd`, `requirements.txt`.
- **Pros**:
  - Live constant test case for create → clone → push → browse.
  - A single repo holds code, ops scripts, init service and doc.
- **Cons**:
  - A dogfood repo can be deleted via its own API; the UI confirm
    guard is the protection. Accepted.
- **Alternatives**:
  - Keep source only in local workspace: rejected — no versioned
    history for ops.
- **References**: `30ffcc8` (repo creation), all later commits.

---

### D10 — Lesson: never run worktree ops on a bare repo

- **Problem**: The README update (`da831ea`, "docs: README update")
  was performed by checking out a work tree against the bare repo
  (`git --work-tree=... checkout`), which registered all other files
  as **deleted** and committed the deletions. Files were restored by
  `24badbf` ("fix: restore files removed by README-only botched
  commit") by re-checking-out the previous tree + copying the new
  README, then `add -A` + commit. Final verification: all 8 files
  identical between local workspace and server repo (git hash-object
  match per file).
- **Decided operating rule**:
  - Never run `git checkout`/`git worktree` against
    `/home/git/repos/<x>.git` directly.
  - Deliver files by explicit copies via scripts (`deploy.sh`,
    `start.sh`) or `git clone` into a scratch work tree → commit →
    push back to the bare repo.
  - When updating docs, use explicit copy + add in a scratch clone
    to avoid affecting the rest of the tree.
- **References**: `da831ea` (botched), `24badbf` (restore), final
  state verified identical.

---

## 3. Commit history (chronological, verified)

| Hash | Message |
|------|---------|
| `30ffcc8` | docs: full README (endpoints, deploy, known behavior) / initial service commit |
| `cadf414` | docs: README with endpoints, deploy, known behavior |
| `2734307` | feat: web UI to browse repos/files (index, tree, blob, log) via GET routes |
| `2ab32fe` | feat: render README.md on repo page via marked+DOMPurify CDN |
| `60b3ed4` | feat: create-repo form on index page with JS confirm + fetch |
| `5908900` | feat: delete repository button on repo page with confirmation |
| `c726969` | ops: run server under OpenRC so it survives container reboot |
| `a1e5e64` | ops: start.sh now uses OpenRC instead of setsid+pkill |
| `da831ea` | docs: README update (botched — deleted files via worktree on bare) |
| `24badbf` | fix: restore files removed by README-only botched commit |

## 4. Decision summary

| # | Decision | Status |
|---|----------|--------|
| D1 | LXC container 109 + Alpine 3.23.5 (192.168.2.163) | Accepted |
| D2 | SSH transport, bare repos, user `git` | Accepted |
| D3 | stdlib-only Python HTTP service (no deps) | Accepted |
| D4 | Client-side README rendering via CDN | Accepted |
| D5 | Create-repo form + delete button (confirm-guarded) | Accepted |
| D6 | `-b main` bare repos + client `git branch -m main` | Accepted |
| D7 | OpenRC service manager (reboot-safe) | Accepted |
| D8 | Run as `git`; regex + safe_subpath; no root | Accepted |
| D9 | Self-versioned dogfood repo | Accepted |
| D10 | No worktree ops on bare repo; copy-based deploy | Accepted |

---

*Generated 2026-08-17. Facts verified against the running service and
the `git-server` repo history.*