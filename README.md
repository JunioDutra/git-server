# git-http-server

HTTP service to create bare git repositories on this server (LXC 109, Alpine).

## Endpoint

`POST /create` — body `{"name": "my-repo"}` (JSON or form)

- Creates bare repo at `/home/git/repos/<name>.git` with `git init --bare -b main`
- Validates name (regex, no path traversal)
- Returns `201` (created), `400` (invalid name), `409` (already exists)

`GET /` — service info.

## Deploy / Start

```bash
bash start.sh   # stops old process, starts as git user (setsid, detached)
```

**Critical**: server MUST run as user `git`. If run as root, created repos are
root-owned and SSH push fails with "dubious ownership".

## Files

- `app.py` — stdlib-only Python HTTP server (no deps)
- `start.sh` — stop old + start as git user
- `deploy.sh` — install python3/curl + copy app.py into container
- `check.sh` — health check endpoint
- `flow.sh` — end-to-end test: create via API → clone SSH → push → verify

## Known behavior

Cloning an empty repo defaults local branch to `master` even though the server
creates bare repos with `-b main`. Client must `git branch -m main` before push.