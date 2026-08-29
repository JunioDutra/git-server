# ADR-0004: Multiple builds and external infrastructure configuration

- **Status**: Accepted
- **Date**: 2026-08-28
- **Context**: Multi-image repositories and authenticated internal registry

## Context

ADR-0002 allowed one top-level `dockerfile:` and embedded registry, BuildKit,
and builder destinations in the production script. Repositories now need to
publish multiple independently named images, while infrastructure addresses and
credentials must remain deployment configuration rather than source policy.

## Decisions

1. `repository.yaml` uses a `build` list. Every item requires a lowercase OCI
   `name`, repository-relative `context`, and a `dockerfile` relative to that
   context. An optional string map `args` becomes redacted `--build-arg`
   arguments. The legacy `dockerfile:` key is rejected immediately.
2. Images are `${REGISTRY_ADDRESS}/<repo>/<name>:<short-sha>` and receive
   `:latest` only on root `default_branch`, with `${GIT_DEFAULT_BRANCH}` as the
   fallback.
3. Items execute sequentially and independently. All items are attempted; any
   failure makes the worker unsuccessful. Each item has its own hook log.
4. YAML is parsed with `PyYAML.safe_load`. The full configuration and all paths
   are validated before build execution.
5. Registry and BuildKit settings are required environment variables. Registry
   credentials use an isolated temporary Docker config and password stdin. HTTP
   transport is enabled only when `REGISTRY_INSECURE=true`.
6. The canonical worker is versioned as `build_image.py`. Global configuration,
   authentication, or builder failures receive diagnostic logs.
7. Root configuration accepts only `build`, `mirrors`, and `default_branch`;
   unknown root or build fields fail before execution.
8. Runtime and operational infrastructure values are documented in
   `.env.example`. The original global environment and dispatch model is
   superseded by ADR-0005; registry credentials are not inherited by
   SSH/git-shell processes.

## Consequences

- A monorepo can publish several collision-free images from one push.
- Repositories control their release branch without changing server-wide
  defaults, and non-secret build args stay out of command logs.
- Schema mistakes fail before partial publication, while runtime failure in one
  valid item does not hide the outcome of later items.
- Deployments must provision the required environment and `py3-yaml` before
  activating the worker.
- ADR-0005 confines registry credentials to a dedicated build service.
- ADR-0002 remains authoritative for hook triggering and remote-build topology,
  but its single-build schema and fixed endpoint examples are superseded. IPs
  retained in older ADRs describe historical deployments, not active defaults.
