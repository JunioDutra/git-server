# ADR-0006: Post-build shell tasks and managed repository variables

- **Status**: Proposed
- **Date**: 2026-08-30
- **Context**: Complete build-and-deploy workflows from `repository.yaml` without committing secrets

## Context

`repository.yaml` currently declares a repository's default branch, container
image builds, and mirror destinations. A branch push is received by the
canonical `post-receive` hook, submitted to the durable dispatcher, and handled
asynchronously by `build_image.py`. That worker already materializes the exact
pushed commit in a temporary directory, validates the complete configuration,
executes every build, and records per-execution logs.

Repositories also need a final deployment or update phase. Typical operations
include calling a deployment tool, connecting to another host, or running a
repository-owned script after its images have been published. Keeping these
operations outside GitServer requires a second CI system and leaves the existing
build pipeline incomplete.

Inline shell execution introduces a stronger trust boundary than image builds.
A script is arbitrary code from the pushed commit and will have network access
and the permissions of its operating-system user. It may also need sensitive
values such as `SSH_DEPLOY_KEY`. Those values must not be committed to
`repository.yaml`, returned to the browser after creation, inherited from the
dispatcher's infrastructure environment, or written to hook logs.

The HTTP application intentionally does not implement authentication because it
is deployed behind the existing Nginx Proxy, which is the authentication and
TLS boundary for the management interface. The new variable-management surface
must remain reachable only through that proxy. Direct access to the application
origin would bypass the system's security boundary and is therefore prohibited.
CSRF protection appropriate to the proxy's browser authentication mechanism is
also required for secret mutations.

This feature is inspired by the usability of GitHub Actions' inline `run`
steps, but it is not an implementation of the GitHub Actions workflow format or
runner isolation model. GitServer remains a small, push-triggered homelab
service.

## Decisions

1. The root of `repository.yaml` accepts a new `tasks` list. Each task requires
   a unique `name` and a non-empty `run` string. It may declare an optional list
   of exact branch names and an optional bounded timeout:

   ```yaml
   default_branch: master

   build:
     - name: api
       context: .
       dockerfile: apps/api/Dockerfile

     - name: web
       context: .
       dockerfile: apps/web/Dockerfile
       args:
         VITE_API_URL: "https://example.internal"

   tasks:
     - name: post-update
       branches: [master]
       timeout_seconds: 900
       run: |
         echo "Starting deploy process..."
         node --version
         echo "Deploy process complete."

   mirrors:
     - url: git@github.com:JunioDutra/da-school.git
   ```

   `branches` is omitted to run on every non-deletion branch push, matching the
   existing hook trigger. Branch values are validated with Git. The initial
   schema does not implement events, expressions, matrices, dependencies, or
   arbitrary GitHub Actions properties. If `tasks` is present it must be a
   non-empty list, task names must be unique, unknown fields are rejected, and
   script size and timeout have server-defined upper bounds.

2. Tasks are part of the existing durable dispatcher job and execute against
   the same temporary snapshot of the exact pushed SHA used by the build. They
   run sequentially, in declaration order, after all configured builds finish
   successfully. A repository with `tasks` and no `build` runs its matching
   tasks immediately after configuration validation. If configuration or any
   build fails, no task runs. If a task fails or times out, later tasks are
   skipped and the worker reports failure. Push acceptance remains asynchronous
   and is never rolled back by a build or task failure.

3. Each task runs with `/bin/sh -eu -c <run>`, the repository snapshot as its
   working directory, `stdin` connected to `/dev/null`, and `umask 077`. It runs
   in its own process group so a timeout can terminate child processes as well
   as the shell. The first version has one supported shell; selecting images,
   operating systems, or alternate shells requires a later decision.

4. The task environment is constructed from a strict allowlist rather than
   copied from the dispatcher process. It contains only safe runtime basics,
   GitServer-provided immutable metadata, and the current repository's managed
   variables. Initial metadata includes:

   - `GIT_SERVER_REPOSITORY`;
   - `GIT_SERVER_BRANCH`;
   - `GIT_SERVER_SHA`;
   - `GIT_SERVER_SHORT_SHA`;
   - `GIT_SERVER_DEFAULT_BRANCH`.

   Registry credentials, BuildKit settings, dispatcher paths, and other service
   configuration are never inherited by shell tasks. User variable names must
   match `[A-Z_][A-Z0-9_]*`; GitServer metadata names and service-owned names or
   prefixes are reserved and cannot be overridden.

5. Managed variables are repository-scoped server state stored outside every
   Git repository and outside `repository.yaml`. All managed values are treated
   as secrets, including values that are not intrinsically sensitive. The
   storage root is deployment configuration, defaulting to
   `/home/git/repository-env`. Its directory and per-repository files use the
   most restrictive ownership and modes compatible with the HTTP service and
   dispatcher. Updates use a lock and an atomic write, including flush and
   replacement, so a crash cannot leave a partially written secret set.

6. The HTTP API never has a read-value operation. Its read model returns only a
   variable name and `configured: true`. The browser renders a configured value
   as `***` and uses an empty password input with that placeholder. Saving an
   untouched row sends no value and preserves the stored value. A submitted new
   value replaces only that variable. Deletion is an explicit operation; an
   empty string or the literal placeholder is not interpreted as deletion.
   Consequently, editing one variable cannot blank or replace its siblings.

   A patch follows this semantic shape:

   ```json
   {
     "upsert": {"SSH_DEPLOY_KEY": "new value supplied by the operator"},
     "delete": ["OBSOLETE_TOKEN"]
   }
   ```

   Responses, validation errors, application logs, and audit records may include
   variable names but never values. Deleting a repository also deletes its
   managed variable file.

7. Variable list and mutation pages and endpoints are admin-only through the
   existing Nginx Proxy. The proxy authenticates the operator and terminates
   HTTPS before forwarding a request; GitServer does not duplicate
   authentication or session management inside `app.py`. Network policy and
   service binding must prevent clients from reaching the application origin
   directly. Failed authentication occurs at the proxy before the request is
   forwarded to GitServer. Browser mutations require CSRF protection appropriate
   to the proxy's authentication mechanism, enforced at the proxy or application
   layer. If an authenticated identity is forwarded for audit, Nginx must remove
   any client-supplied copy and set the trusted header itself. The UI must not
   place managed values in HTML, JavaScript, URLs, browser storage, or a response
   payload.

8. Every task receives a separate hook log with task name, repository, branch,
   SHA, timestamps, and exit status. The raw script and environment are not
   repeated in the log. Output is streamed through exact-value redaction for all
   managed variables and is subject to a size limit. Redaction reduces accidental
   disclosure but cannot protect a value transformed, encoded, split, or
   intentionally exfiltrated by the script.

9. Task execution retains ADR-0005's at-least-once delivery. A dispatcher crash
   can cause a task to run again for the same SHA, so deployment scripts must be
   idempotent. The log and documentation expose that contract. GitServer does
   not claim exactly-once deployment.

10. The initial executor is explicitly limited to trusted repositories and
    trusted push access in this single-tenant homelab. A shell task is arbitrary
    code running as a local service user, with network access and visibility
    allowed by that account and the LXC filesystem. Filtering the environment
    prevents accidental inheritance of dispatcher secrets but does not create a
    GitHub-hosted-runner-grade sandbox or strong isolation between concurrently
    executing repositories. Multi-tenant use requires a disposable container or
    VM runner under a separate ADR before shell tasks may be enabled.

## Consequences

### Positive

- **POS-001**: A repository can complete build and deployment from one
  push-triggered, versioned configuration without adding a separate CI system.
- **POS-002**: Inline scripts run from the immutable pushed snapshot after image
  publication, making the deployed logic reviewable alongside the application.
- **POS-003**: Deployment credentials stay server-side and can be rotated from
  the interface without creating a commit or disclosing their previous values.
- **POS-004**: Patch semantics and explicit deletion prevent an edit to one
  variable from unintentionally changing other variables.
- **POS-005**: Task logs integrate with the existing hook-log UI and preserve
  the asynchronous push behavior and durable retry model.
- **POS-006**: A filtered environment prevents repository scripts from
  automatically receiving registry and dispatcher credentials merely because
  they run in the same worker lifecycle.
- **POS-007**: Authentication and TLS remain centralized in the existing Nginx
  Proxy instead of introducing a second identity and session implementation in
  the GitServer application.

### Negative

- **NEG-001**: Anyone able to modify a configured task can execute arbitrary
  commands with the task runner's OS and network permissions on the next
  matching push.
- **NEG-002**: Secret management now depends on the Nginx Proxy and network
  policy being configured correctly; exposing the application origin directly
  would bypass authentication and TLS.
- **NEG-003**: Exact-value log redaction is not a security boundary; a malicious
  script can transform or transmit secrets, and operational logs must still be
  treated as sensitive.
- **NEG-004**: At-least-once recovery can repeat external deployment side
  effects, placing an idempotency requirement on repository authors.
- **NEG-005**: Sequential tasks occupy a dispatcher worker until completion;
  timeouts, output limits, queue visibility, and appropriate worker sizing
  become operational requirements.
- **NEG-006**: Server-side variable files become sensitive backup material and
  require controlled permissions, retention, recovery, and deletion procedures.
- **NEG-007**: Running on the LXC host account is materially less isolated than
  GitHub-hosted runners and is unsuitable for untrusted or multi-tenant repos.

## Alternatives Considered

### Execute scripts directly in `post-receive`

- **ALT-001**: **Description**: Have the Git hook read `tasks` and invoke each
  shell command before returning from the push.
- **ALT-002**: **Rejection reason**: This would block Git transport, expose work
  and credentials to the SSH process boundary, and discard ADR-0005's durable
  queue and controlled worker environment.

### Commit task variables to `repository.yaml`

- **ALT-003**: **Description**: Add an `env` map beside `tasks` and interpolate
  its values into each command.
- **ALT-004**: **Rejection reason**: Deployment keys and tokens would enter Git
  history, clones, browser views, and mirrors, and rotation would require a
  commit without removing the historical disclosure.

### Use only global dispatcher environment variables

- **ALT-005**: **Description**: Provision every repository's values in the
  OpenRC service environment and pass the complete environment to scripts.
- **ALT-006**: **Rejection reason**: This provides no repository scoping, risks
  exposing registry credentials to arbitrary scripts, requires service-level
  operations for every rotation, and recreates the coupling removed by
  ADR-0005.

### Implement the GitHub Actions workflow format

- **ALT-007**: **Description**: Support `on`, `jobs`, `steps`, expressions,
  actions, matrices, and runner labels for compatibility with GitHub Actions.
- **ALT-008**: **Rejection reason**: The compatibility surface and isolation
  requirements are far larger than the requested post-build hook. A small,
  explicit schema is auditable and matches the current GitServer architecture.

### Run every task in a disposable container from the first release

- **ALT-009**: **Description**: Add a rootless OCI runtime and execute each task
  in a short-lived container with only its snapshot and selected variables.
- **ALT-010**: **Rejection reason**: This is the preferred direction for
  multi-tenant isolation, but the repository currently has no general-purpose
  runtime contract, base image selection, cache model, or deploy-network model.
  Introducing all of them is a separate capability. The first release is
  restricted to trusted repositories and documents that limitation.

### Keep deployments outside GitServer

- **ALT-011**: **Description**: Continue using GitServer only to build images and
  configure deployments manually or in another CI system.
- **ALT-012**: **Rejection reason**: This leaves the build-and-deploy workflow
  incomplete and duplicates the push trigger, logging, credentials, and
  operational state already present in GitServer.

### Implement a second authentication system in GitServer

- **ALT-013**: **Description**: Add login, credential verification, and session
  management directly to `app.py` for the managed-variable pages.
- **ALT-014**: **Rejection reason**: Authentication and TLS are already enforced
  by the Nginx Proxy. Duplicating them would create two security configurations,
  two session lifecycles, and additional secret state without improving the
  boundary, provided direct origin access remains blocked.

## Implementation Notes

- **IMP-001**: Extend the central parser so `build`, `tasks`, `mirrors`, and
  `default_branch` are validated together before any build or task starts. Add
  tests for unknown fields, duplicate names, invalid branches, empty scripts,
  oversized scripts, and timeout bounds.
- **IMP-002**: Refactor the worker lifecycle into configuration/materialization,
  build, and task phases without changing the dispatcher request schema. Preserve
  independent build attempts, but gate the complete task phase on aggregate
  build success.
- **IMP-003**: Implement task subprocesses with a new process group, monotonic
  timeout, group termination, non-interactive stdin, filtered environment,
  bounded log output, and exact secret redaction. Tests must verify that service
  credentials are absent from the child environment and logs.
- **IMP-004**: Add a repository-variable store with atomic patch operations and
  strict name validation. Cover create, replace-one-preserve-others, explicit
  delete, concurrent update, malformed storage, repository deletion, file modes,
  and multiline values such as an SSH private key.
- **IMP-005**: Keep authentication and HTTPS termination in the Nginx Proxy.
  Deployment checks must verify that the application origin is inaccessible to
  clients, unauthenticated proxy requests are rejected before forwarding, and
  browser mutations have effective CSRF protection. Security tests must also
  confirm that values never appear in list responses, HTML, application logs,
  error messages, URLs, or unchanged edit submissions.
- **IMP-006**: Document that tasks run on the GitServer LXC rather than an
  Ubuntu GitHub runner. Required binaries such as `node`, `ssh`, or deployment
  CLIs must be installed and versioned as deployment prerequisites.
- **IMP-007**: Add an end-to-end acceptance case that configures a masked
  multiline variable, builds an image, runs a default-branch task, observes a
  redacted per-task log, edits only a second variable, and verifies the first
  value remains unchanged.

## References

- **REF-001**: [ADR-0004: Multiple builds and external infrastructure configuration](ADR-0004-multiple-builds-and-external-configuration.md)
- **REF-002**: [ADR-0005: Isolated durable build dispatcher](ADR-0005-isolated-durable-build-dispatcher.md)
- **REF-003**: [ADR-0003: Automatic mirror sync via Git hooks](ADR-0003-automatic-mirror-sync-via-git-hooks.md)
- **REF-004**: [`build_image.py`](../../build_image.py), current parser and build worker
- **REF-005**: [`build_dispatcher.py`](../../build_dispatcher.py), durable at-least-once execution
- **REF-006**: [`app.py`](../../app.py), HTTP management application deployed behind the authenticated Nginx Proxy
- **REF-007**: [`post-receive`](../../post-receive), current asynchronous push trigger
