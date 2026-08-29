# git-http-server

HTTP service for creating and browsing bare Git repositories, running as user
`git` in an Alpine LXC. Server-side `post-receive` hooks can mirror refs and
build multiple container images through a remote BuildKit daemon.

## Configuration

Infrastructure destinations and credentials are never hard-coded. Runtime
variables must be present in the LXC environment. The deploy validates their
presence in a fresh container process without displaying values, then writes
the build-only values to the protected OpenRC configuration
`/etc/conf.d/git-build-dispatcher` (`0640 root:git`). Registry credentials are
not inherited by SSH/git-shell processes.

Copy `.env.example` to `.env` only for local operational scripts. `.env` is
ignored by Git, and its path can be changed with `GIT_SERVER_OPS_ENV_FILE`.

```bash
cp .env.example .env
chmod 0600 .env
```

The HTTP application and hook consume only non-secret inherited settings. The
build dispatcher consumes its service-scoped OpenRC configuration. No
repository-managed environment file is loaded.

Required runtime variables:

| Variable | Purpose |
|---|---|
| `GIT_HTTP_HOST` / `GIT_HTTP_PORT` | HTTP listener |
| `GIT_SSH_HOST` | Host shown in clone commands |
| `GIT_DEFAULT_BRANCH` | New-repo branch and fallback for repository `latest` |
| `REGISTRY_ADDRESS` | Registry host and port, without scheme |
| `REGISTRY_USER` / `REGISTRY_PASSWORD` | Registry Basic Authentication |
| `REGISTRY_INSECURE` | `true` for HTTP, `false` for TLS |
| `BUILDKIT_ADDRESS` | Remote BuildKit endpoint |
| `BUILDX_BUILDER` | Name of the remote buildx builder |

Operational scripts additionally require `PROXMOX_HOST`, `PROXMOX_USER`,
`GIT_CONTAINER_ID`, and, for `flow.sh`, `GIT_CONTAINER_IP` and
`GIT_TEST_SSH_KEY`. See [.env.example](.env.example) for the complete template.

Paths and retention can be overridden with `GIT_REPOS_ROOT`, `GIT_HOOKS_ROOT`,
`GIT_HOOK_LOGS_ROOT`, and `GIT_HOOK_LOG_RETENTION_DAYS`. The durable build queue
also accepts `GIT_BUILD_QUEUE_ROOT` (default `/home/git/build-queue`),
`GIT_BUILD_QUEUE_SIZE` (default `100`), `GIT_BUILD_WORKERS` (default `1`), and
`GIT_BUILD_WORKER` (default `/home/git/hooks/build_image.py`).

## Isolated build dispatch

The build path deliberately crosses a small privilege boundary:

```text
git push -> post-receive -> build_submit.py -> Unix socket
                                             |
                         git-build-dispatcher (OpenRC, user git)
                                             |
                         durable .job -> build_image.py -> BuildKit/registry
```

The hook submits only repository, branch, and commit SHA and waits at most two
seconds for queue acknowledgement. The dispatcher validates the bounded JSON
request and repository path, atomically persists it under
`/home/git/build-queue`, then runs the build using its isolated credentials.
The `.job` file is deleted only after the worker returns; pending files are
requeued at startup, so accepted work survives a dispatcher restart with
at-least-once semantics. A repeated job is safe because it publishes the same
commit-derived tag.

If submission fails, the Git push still succeeds and a `build-dispatcher`
diagnostic entry appears in the repository hook logs. Dispatcher service output
is in `/home/git/logs/build-dispatcher.log`.

## Multiple builds

A repository opts into builds with a root `repository.yaml`:

```yaml
default_branch: master

build:
  - name: api
    context: services/api
    dockerfile: Dockerfile
    args:
      APP_ENV: "production"
      PORT: "8080"

  - name: web
    context: services/web
    dockerfile: docker/Dockerfile

mirrors:
  - url: git@github.com:example/project.git
```

Every build object requires `name`, `context`, and `dockerfile`; `args` is an
optional string-to-string map. `dockerfile` is relative to `context`. ARG names
must be Docker identifiers. Names must be lowercase OCI-compatible components;
duplicate names, absolute paths, traversal, missing files, unknown root/build
fields, non-string ARG values, and an empty build list are rejected before any
build starts. The only accepted root fields are `build`, `mirrors`, and
`default_branch`.

The former top-level `dockerfile:` property is not supported. A repository
without `build` is treated as build-disabled and may still declare mirrors.

Each item publishes:

- `${REGISTRY_ADDRESS}/<repo>/<name>:<short-sha>` for every branch;
- `${REGISTRY_ADDRESS}/<repo>/<name>:latest` only on the repository
  `default_branch`, falling back to `GIT_DEFAULT_BRANCH` when omitted.

Builds run sequentially. Failure in one item does not prevent later items from
running, but the worker exits unsuccessfully if any item fails. Authentication
uses a temporary `DOCKER_CONFIG` and `--password-stdin`; credentials are not
written to logs. The same authentication is available to BuildKit for private
base-image pulls and to crane for pushes. Build args are non-secret by contract;
their values are redacted from the recorded command.

## Hook logs

Each build item and mirror execution receives its own file under
`/home/git/logs/hooks/<repo>/`. Build configuration, authentication, or builder
failures create a diagnostic entry. Logs are retained for 30 days by default.

The repository page links to:

- `GET /repo/<name>/hook-logs` — execution list;
- `GET /repo/<name>/hook-logs/<id>` — escaped detail view;
- `GET /repo/<name>/hook-logs/<id>/raw` — complete download;
- `DELETE /repo/<name>/hook-logs/<id>` — delete one log;
- `DELETE /repo/<name>/hook-logs` — delete all logs for the repository.

Historical aggregate build/mirror files appear separately under “Legacy
aggregate logs”. Deleting a repository also deletes its logs.

## HTTP API and browser

- `POST /create` with `{"name":"my-repo"}` creates a bare repository using
  `GIT_DEFAULT_BRANCH` and installs the canonical hook.
- `GET /` lists repositories.
- `GET /repo/<name>/` browses the root tree.
- `GET /repo/<name>/tree/<path>` and `/blob/<path>` browse content.
- `GET /repo/<name>/log` shows recent commits.
- `DELETE /repo/<name>` deletes the repository and its hook logs.

Browse routes accept `?ref=<branch>`. Markdown uses marked and DOMPurify from
jsDelivr with a raw-text fallback. Repository and browse paths are validated
against traversal.

## Deploy and operations

Configure the LXC runtime environment and the local operational `.env`. Restart
the LXC after changing its environment so PID 1 and new `pct exec` processes see
the values, then deploy:

```bash
./deploy.sh
./check.sh
```

`deploy.sh` derives sources from its own directory, installs Python/PyYAML,
deploys the HTTP application, both OpenRC services, the submission client,
dispatcher, and canonical hook workers, refreshes hook symlinks, validates
syntax, and keeps timestamped backups. It removes the old global credential
allowlist, generates the protected dispatcher configuration, restarts the
dispatcher and HTTP service (not SSH), and verifies both HTTP and the Unix
socket.

After an LXC reboot, OpenRC starts `git-build-dispatcher` and `git-http-server`
automatically. No manual SSH restart is required. Useful diagnostics:

```bash
rc-service git-build-dispatcher status
test -S /run/git-server/build.sock
tail -50 /home/git/logs/build-dispatcher.log
```

Other commands:

```bash
./start.sh   # restart/status through Proxmox
./flow.sh    # destructive E2E create → clone → commit → push
```

The HTTP service must run as `git`; running it as root causes ownership and
“dubious ownership” failures on SSH pushes.

## Files

- `app.py` — HTTP API and web UI.
- `build_image.py` — multi-build parser/orchestrator.
- `build_submit.py` — credential-free Unix socket client used by the hook.
- `build_dispatcher.py` — isolated durable queue and worker dispatcher.
- `configure_build_env.py` — protected OpenRC configuration generator.
- `post-receive` — hook dispatcher and mirror trigger.
- `mirror-sync.sh` — mirror worker.
- `git-http-server.initd` — OpenRC service.
- `git-build-dispatcher.initd` — isolated build OpenRC service.
- `.env.example` — configuration contract without secrets.
- `deploy.sh`, `start.sh`, `check.sh`, `flow.sh` — operational scripts.
