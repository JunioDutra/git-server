# git-http-server

HTTP service to create bare git repositories on this server (LXC 109, Alpine).

## Endpoints

`POST /create` — body `{"name": "my-repo"}` (JSON or form)

- Creates bare repo at `/home/git/repos/<name>.git` with `git init --bare -b main`
- Validates name (regex, no path traversal)
- Returns `201` (created), `400` (invalid name), `409` (already exists)

`GET /` — HTML index listing all repositories.

`GET /repo/<name>/` — browse the default repo branch (root).

`GET /repo/<name>/tree/<path>` — browse a subdirectory.

`GET /repo/<name>/blob/<path>` — view file content (plain, escaped).

`GET /repo/<name>/log` — recent commits (plain list).

Browse routes accept `?ref=<branch>`. Repository, file and log pages include a
branch selector and preserve the selected branch in navigation links. If a bare
repository has a dangling `HEAD`, the UI falls back to `main`, `master`, or the
first available branch instead of incorrectly reporting that it is empty.

If a `README.md` (or `README`, `README.txt`, etc.) exists in the repo root, it is
rendered at the bottom of the repo page using `marked` + `DOMPurify` loaded from
jsDelivr CDN (no local JS payload). Relative links in the README are rewritten to
blob URLs on the same server; external links pass through. Content is escaped into
a hidden `<pre>` and rendered client-side, so no `pip` deps are needed server-side.

All browse routes are stdlib-only (`http.server` + `git ls-tree`/`git show`),
no dependencies. Path traversal (`..`) is rejected.

The index page also has a **create-repo form** (input + Create button). Submitting
shows a native JS `confirm()` dialog before POSTing to `/create`; on success the
page redirects to the new repo. Same validation as the API: `[A-Za-z0-9._-]+`,
rejects duplicates with 409.

Each repo page has a **Delete repository** button (red). It shows a native JS
`confirm()` dialog with a "cannot be undone" warning, then `DELETE /repo/<name>`.
On success it redirects back to the index. Works for empty repos too (no commits
yet). API: `DELETE /repo/<name>` returns JSON.

## Deploy / Start

```bash
bash start.sh   # stops old process, starts as git user (setsid, detached)
```

**Critical**: server MUST run as user `git`. If run as root, created repos are
root-owned and SSH push fails with "dubious ownership".

## Files

- `app.py` — stdlib-only Python HTTP server (no deps). Includes repo creation API + web UI to browse repos/files.
- `start.sh` — stop old + start as git user
- `deploy.sh` — install python3/curl + copy app.py into container
- `check.sh` — health check endpoint
- `flow.sh` — end-to-end test: create via API → clone SSH → push → verify

## Known behavior

Cloning an empty repo defaults local branch to `master` even though the server
creates bare repos with `-b main`. Client must `git branch -m main` before push.

## Repository

add mirror configuration
