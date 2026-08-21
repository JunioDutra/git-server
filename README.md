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

`GET /repo/<name>/blob/<path>` — view file content. Markdown files (`.md` and
`.markdown`) open rendered and can be toggled to raw text.

`GET /repo/<name>/log` — recent commits (plain list).

`GET /repo/<name>/hook-logs` — history of build and mirror hook executions.

- `GET /repo/<name>/hook-logs/<id>` displays a log safely; large logs show the
  last 1 MiB and link to `GET /repo/<name>/hook-logs/<id>/raw` for download.
- `DELETE /repo/<name>/hook-logs/<id>` removes one execution log.
- `DELETE /repo/<name>/hook-logs` removes all execution logs for the repo.

Each asynchronous build or mirror invocation receives its own file under
`/home/git/logs/hooks/<repo>/`. The dispatcher records its type, start time,
branch/SHA or refs, and final exit code. The UI keeps these files for 30 days
by default; configure `GIT_HOOK_LOGS_ROOT` and `GIT_HOOK_LOG_RETENTION_DAYS`
to change storage location and retention. Existing aggregate files in
`/home/git/logs/builds/` and `/home/git/logs/mirrors/` appear as legacy entries
until removed. Deleting a repository also removes all of its hook logs.

Browse routes accept `?ref=<branch>`. Repository, file and log pages include a
branch selector and preserve the selected branch in navigation links. If a bare
repository has a dangling `HEAD`, the UI falls back to `main`, `master`, or the
first available branch instead of incorrectly reporting that it is empty.

If a `README.md` (or `README`, `README.txt`, etc.) exists in the repo root, it is
rendered at the bottom of the repo page using `marked` + `DOMPurify` loaded from
jsDelivr CDN (no local JS payload). Relative links in the README are rewritten to
blob URLs on the same server; external links pass through. Content is escaped into
a hidden `<pre>` and rendered client-side, so no `pip` deps are needed server-side.
The README is only shown on the repository root, never when browsing a subdirectory.

At the top of every repository root, the UI shows copyable SSH commands using the
`git-server` hostname for both cloning and adding the repository as `origin`.

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

Every repository root also has a **hook logs** link. The list allows viewing or
downloading each execution, deleting a selected log, or clearing all logs after
confirmation. A missing end marker is shown as running/incomplete, which also
makes interrupted background tasks visible.

## Atualizar o servidor

O deploy é manual e passa pelo host `idaemon`, que possui acesso administrativo ao
Proxmox e ao container 109. As credenciais e chaves devem estar configuradas fora
deste repositório; estes comandos não criam, copiam nem exibem chaves.

Depois de atualizar o código local, valide a sintaxe e envie a nova versão como um
arquivo temporário, sem interromper o serviço:

```bash
python3 -c "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')"
ssh idaemon 'ssh root@192.168.2.150 "pct exec 109 -- sh -c '\''cat > /opt/git-http-server/app.py.new'\''"' < app.py
```

Entre no salto e ative a versão. O procedimento valida o arquivo, guarda um backup
com data e hora, troca o app e reinicia somente o serviço HTTP:

```bash
ssh idaemon
ssh root@192.168.2.150
pct exec 109 -- python3 -m py_compile /opt/git-http-server/app.py.new
pct exec 109 -- sh -c 'set -e; stamp=$(date +%Y%m%d-%H%M%S); cp -p /opt/git-http-server/app.py /opt/git-http-server/app.py.bak-$stamp; chown git:git /opt/git-http-server/app.py.new; chmod 0644 /opt/git-http-server/app.py.new; mv /opt/git-http-server/app.py.new /opt/git-http-server/app.py; rc-service git-http-server restart'
pct exec 109 -- rc-service git-http-server status
pct exec 109 -- sh -c 'curl -fsS http://127.0.0.1:8080/ >/dev/null || wget -qO- http://127.0.0.1:8080/ >/dev/null'
exit
exit
```

The repository `deploy.sh` also installs the canonical `post-receive` and
`mirror-sync.sh` into `/home/git/hooks`, creates `/home/git/logs/hooks` as
`git:git`, and refreshes the hook symlink in every existing bare repository.
New repositories receive the same symlink during creation. The deploy does not
replace the separately managed `build-image.sh`.

Para rollback, liste os backups no container e substitua `BACKUP` pelo arquivo que
quer restaurar:

```bash
pct exec 109 -- sh -c 'ls -1t /opt/git-http-server/app.py.bak-*'
pct exec 109 -- sh -c 'cp -p /opt/git-http-server/BACKUP /opt/git-http-server/app.py && chown git:git /opt/git-http-server/app.py && rc-service git-http-server restart'
```

## Start

```bash
bash start.sh   # stops old process, starts as git user (setsid, detached)
```

**Critical**: server MUST run as user `git`. If run as root, created repos are
root-owned and SSH push fails with "dubious ownership".

## Files

- `app.py` — stdlib-only Python HTTP server (no deps), including hook-log browser and deletion API.
- `post-receive` — canonical dispatcher that records a separate log for each build or mirror task.
- `mirror-sync.sh` — mirror worker; its stdout/stderr is captured by the dispatcher.
- `start.sh` — stop old + start as git user
- `deploy.sh` — install python3/curl + copy app.py into container
- `check.sh` — health check endpoint
- `flow.sh` — end-to-end test: create via API → clone SSH → push → verify

## Known behavior

Cloning an empty repo defaults local branch to `master` even though the server
creates bare repos with `-b main`. Client must `git branch -m main` before push.

## Repository

add mirror configuration
