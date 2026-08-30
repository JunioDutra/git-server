#!/usr/bin/env python3
"""Write a protected OpenRC environment for the isolated build service."""

import os
import pathlib
import shlex
import tempfile


REQUIRED = (
    "REGISTRY_ADDRESS",
    "REGISTRY_USER",
    "REGISTRY_PASSWORD",
    "REGISTRY_INSECURE",
    "BUILDKIT_ADDRESS",
    "BUILDX_BUILDER",
    "GIT_DEFAULT_BRANCH",
)
OPTIONAL = (
    "GIT_REPOS_ROOT",
    "GIT_HOOK_LOGS_ROOT",
    "GIT_HOOK_LOG_RETENTION_DAYS",
    "GIT_BUILD_SOCKET",
    "GIT_BUILD_QUEUE_ROOT",
    "GIT_BUILD_QUEUE_SIZE",
    "GIT_BUILD_WORKERS",
    "GIT_BUILD_WORKER",
    "GIT_REPOSITORY_ENV_ROOT",
)


def render(environ):
    missing = [name for name in REQUIRED if not environ.get(name)]
    if missing:
        raise ValueError(f"missing required environment variables: {', '.join(missing)}")
    names = REQUIRED + tuple(name for name in OPTIONAL if environ.get(name))
    return "".join(f"export {name}={shlex.quote(environ[name])}\n" for name in names)


def install(environ=None, destination="/etc/conf.d/git-build-dispatcher"):
    import grp

    environ = dict(os.environ if environ is None else environ)
    content = render(environ)
    target = pathlib.Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        os.chown(temporary, 0, grp.getgrnam("git").gr_gid)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    install()
