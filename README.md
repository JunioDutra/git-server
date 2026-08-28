# git-http-server

HTTP service for creating and browsing bare Git repositories, running as user
`git` in an Alpine LXC. Server-side `post-receive` hooks can mirror refs and
build multiple container images through a remote BuildKit daemon.

## Configuration

Infrastructure destinations and credentials are never hard-coded. Copy
`.env.example` to `.env` for local operational scripts and fill the required
values. `.env` is ignored by Git.

```bash
cp .env.example .env
chmod 0600 .env
```

Production hooks and the OpenRC service load `/etc/git-server/build.env` by
default. Override that path with `GIT_SERVER_ENV_FILE`. Provision it manually;
`deploy.sh` validates but never creates, copies, displays, or overwrites it.

```bash
install -d -m 0750 -o root -g git /etc/git-server
install -m 0640 -o root -g git /path/to/build.env /etc/git-server/build.env
```

Required runtime variables:

| Variable | Purpose |
|---|---|
| `GIT_HTTP_HOST` / `GIT_HTTP_PORT` | HTTP listener |
| `GIT_SSH_HOST` | Host shown in clone commands |
| `GIT_DEFAULT_BRANCH` | New-repo branch and branch that receives `latest` |
| `REGISTRY_ADDRESS` | Registry host and port, without scheme |
| `REGISTRY_USER` / `REGISTRY_PASSWORD` | Registry Basic Authentication |
| `REGISTRY_INSECURE` | `true` for HTTP, `false` for TLS |
| `BUILDKIT_ADDRESS` | Remote BuildKit endpoint |
| `BUILDX_BUILDER` | Name of the remote buildx builder |

Operational scripts additionally require `PROXMOX_HOST`, `PROXMOX_USER`,
`GIT_CONTAINER_ID`, and, for `flow.sh`, `GIT_CONTAINER_IP` and
`GIT_TEST_SSH_KEY`. See [.env.example](.env.example) for the complete template.

Paths and retention can be overridden with `GIT_REPOS_ROOT`, `GIT_HOOKS_ROOT`,
`GIT_HOOK_LOGS_ROOT`, and `GIT_HOOK_LOG_RETENTION_DAYS`.

## Multiple builds

A repository opts into builds with a root `repository.yaml`:

```yaml
build:
  - name: api
    context: services/api
    dockerfile: Dockerfile

  - name: web
    context: services/web
    dockerfile: docker/Dockerfile

mirrors:
  - url: git@github.com:example/project.git
```

Every build object requires exactly `name`, `context`, and `dockerfile`.
`dockerfile` is relative to `context`. Names must be lowercase OCI-compatible
components; duplicate names, absolute paths, traversal, missing files, unknown
fields, and an empty build list are rejected before any build starts.

The former top-level `dockerfile:` property is not supported. A repository
without `build` is treated as build-disabled and may still declare mirrors.

Each item publishes:

- `${REGISTRY_ADDRESS}/<repo>/<name>:<short-sha>` for every branch;
- `${REGISTRY_ADDRESS}/<repo>/<name>:latest` only on `GIT_DEFAULT_BRANCH`.

Builds run sequentially. Failure in one item does not prevent later items from
running, but the worker exits unsuccessfully if any item fails. Authentication
uses a temporary `DOCKER_CONFIG` and `--password-stdin`; credentials are not
written to logs. The same authentication is available to BuildKit for private
base-image pulls and to crane for pushes.

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

Configure `.env`, provision `/etc/git-server/build.env` in the container, then:

```bash
./deploy.sh
./check.sh
```

`deploy.sh` derives sources from its own directory, installs Python/PyYAML,
deploys the HTTP application and all canonical hook workers, refreshes hook
symlinks, validates syntax, keeps timestamped backups of replaced files,
restarts OpenRC, and performs a health check.

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
- `post-receive` — asynchronous dispatcher.
- `mirror-sync.sh` — mirror worker.
- `git-http-server.initd` — OpenRC service.
- `.env.example` — configuration contract without secrets.
- `deploy.sh`, `start.sh`, `check.sh`, `flow.sh` — operational scripts.
