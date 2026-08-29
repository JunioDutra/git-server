# ADR-0005: Isolated durable build dispatcher

- **Status**: Accepted
- **Date**: 2026-08-29
- **Context**: Builds triggered by SSH/git-shell need credentials without exposing them to SSH sessions

## Context

The `post-receive` hook runs below the SSH daemon and previously launched the
build worker directly. That coupled build availability to the environment
inherited by `sshd`: registry credentials had to be globally allowed through
OpenRC and an environment change required restarting SSH. It also meant that a
process boundary intended for Git transport received infrastructure secrets.

Build execution must remain asynchronous, survive a dispatcher restart after a
job is accepted, and keep every executable and deployment step versioned in
this repository.

## Decisions

1. `post-receive` submits only `repo`, `branch`, and `sha` to a local Unix
   socket. The operation has a two-second timeout and never invalidates a push.
2. `git-build-dispatcher`, a dedicated OpenRC service running as `git`, validates
   the request, persists it atomically, and invokes `build_image.py` with its
   service-scoped environment.
3. Accepted jobs remain as `.job` files until the worker returns. Pending jobs
   are recovered on service startup, providing at-least-once execution after a
   crash or restart. A configured queue limit prevents unbounded growth.
4. The socket is owned by `git`, has mode `0660`, accepts a bounded JSON line,
   and repository names, branches, SHAs, fields, and resolved repository paths
   are validated before persistence.
5. The deploy generates `/etc/conf.d/git-build-dispatcher` from the inherited
   LXC environment with mode `0640` and owner `root:git`. The generated file is
   machine state, not source; its generator and complete variable contract are
   versioned here.
6. Registry credentials and BuildKit configuration are removed from the global
   OpenRC allowlist. Only non-secret HTTP/Git/path settings may be inherited by
   other processes. Deploy no longer restarts `sshd`.
7. Worker stdout/stderr continues to be written to the per-execution hook logs.
   Dispatcher startup output goes to `/home/git/logs/build-dispatcher.log`.

## Consequences

- SSH/git-shell sessions and hooks no longer require registry credentials.
- A successful submission can execute again after a crash; build publication
  must therefore remain safe to repeat for the same immutable tag.
- If the dispatcher is stopped, full, or unavailable, the push succeeds and a
  `build-dispatcher` diagnostic hook log records the submission failure.
- LXC environment changes become visible to new container processes only after
  the container is restarted. Deploy then regenerates the protected service
  configuration and restarts only the dispatcher and HTTP service.
- The queue is durable across service restarts but remains local to the LXC and
  is removed only by an explicit administrative action.
