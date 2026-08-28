#!/usr/bin/env python3
"""Build every image declared in repository.yaml and log each item separately."""

import datetime as dt
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import yaml


NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
ALLOWED_BUILD_KEYS = {"name", "context", "dockerfile"}
REQUIRED_ENV = (
    "REGISTRY_ADDRESS",
    "REGISTRY_USER",
    "REGISTRY_PASSWORD",
    "REGISTRY_INSECURE",
    "BUILDKIT_ADDRESS",
    "BUILDX_BUILDER",
    "GIT_DEFAULT_BRANCH",
)


class ConfigError(ValueError):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso_now():
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_relative_path(value, field, allow_dot=False):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{field}' must be a non-empty string")
    value = value.strip().replace("\\", "/")
    if any(char in value for char in ("\x00", "\r", "\n", "\t")):
        raise ConfigError(f"'{field}' cannot contain control characters")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"'{field}' must be relative and cannot contain '..': {value}")
    if not allow_dot and value in (".", ""):
        raise ConfigError(f"'{field}' must point to a file")
    return value


def parse_build_config(raw):
    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid repository.yaml: {exc}") from exc
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ConfigError("repository.yaml root must be a mapping")
    if "dockerfile" in config:
        raise ConfigError("legacy 'dockerfile' key is not supported; migrate to 'build' list")
    if "build" not in config:
        return []
    builds = config["build"]
    if not isinstance(builds, list) or not builds:
        raise ConfigError("'build' must be a non-empty list")

    parsed = []
    names = set()
    for index, item in enumerate(builds, start=1):
        label = f"build[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be an object")
        unknown = sorted(set(item) - ALLOWED_BUILD_KEYS)
        missing = sorted(ALLOWED_BUILD_KEYS - set(item))
        if unknown:
            raise ConfigError(f"{label} has unknown fields: {', '.join(unknown)}")
        if missing:
            raise ConfigError(f"{label} is missing fields: {', '.join(missing)}")
        name = item["name"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ConfigError(f"{label}.name must be a lowercase OCI name")
        if name in names:
            raise ConfigError(f"duplicate build name: {name}")
        names.add(name)
        parsed.append({
            "name": name,
            "context": validate_relative_path(item["context"], f"{label}.context", allow_dot=True),
            "dockerfile": validate_relative_path(item["dockerfile"], f"{label}.dockerfile"),
        })
    return parsed


def resolve_build_paths(work, build):
    root = pathlib.Path(work).resolve()
    context = (root / build["context"]).resolve()
    dockerfile = (context / build["dockerfile"]).resolve()
    try:
        context.relative_to(root)
        dockerfile.relative_to(context)
    except ValueError as exc:
        raise ConfigError(f"build '{build['name']}' escapes its repository context") from exc
    if not context.is_dir():
        raise ConfigError(f"build '{build['name']}' context not found: {build['context']}")
    if not dockerfile.is_file():
        raise ConfigError(
            f"build '{build['name']}' dockerfile not found: "
            f"{build['context']}/{build['dockerfile']}"
        )
    return context, dockerfile


def validate_environment(environ):
    missing = [name for name in REQUIRED_ENV if not environ.get(name)]
    if missing:
        raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
    insecure = environ["REGISTRY_INSECURE"].strip().lower()
    if insecure not in ("true", "false"):
        raise ConfigError("REGISTRY_INSECURE must be 'true' or 'false'")
    registry = environ["REGISTRY_ADDRESS"]
    if "://" in registry or "/" in registry or any(char.isspace() for char in registry):
        raise ConfigError("REGISTRY_ADDRESS must be a host[:port] without scheme or path")
    return insecure == "true"


class ExecutionLog:
    def __init__(self, log_dir, kind, metadata):
        pathlib.Path(log_dir).mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        safe_kind = re.sub(r"[^a-z0-9._-]+", "-", kind.lower())
        fd, self.path = tempfile.mkstemp(prefix=f"{stamp}-{safe_kind}-", suffix=".log", dir=log_dir)
        self.file = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        self.write_meta("type", kind)
        for key, value in metadata.items():
            self.write_meta(key, value)
        self.write_meta("started_at", iso_now())
        self.file.write("# hook-log: --- output ---\n")

    def write_meta(self, key, value):
        self.file.write(f"# hook-log: {key}={value}\n")

    def finish(self, code):
        self.write_meta("ended_at", iso_now())
        self.write_meta("exit", code)
        self.file.close()


def run_command(command, log, env=None, stdin_text=None):
    printable = ["***" if part == os.environ.get("REGISTRY_PASSWORD") else part for part in command]
    log.file.write(f"command: {shlex.join(printable)}\n")
    try:
        proc = subprocess.run(
            command,
            input=stdin_text,
            text=True,
            stdout=log.file,
            stderr=subprocess.STDOUT,
            env=env,
        )
        return proc.returncode
    except OSError as exc:
        log.file.write(f"cannot execute {command[0]}: {exc}\n")
        return 127


def diagnostic_log(log_dir, kind, repo, branch, sha, message, code=1):
    log = ExecutionLog(log_dir, kind, {"repo": repo, "branch": branch, "sha": sha})
    log.file.write(f"{message}\n")
    log.finish(code)
    return code


def materialize_tree(repo_bare, sha, work):
    archive = subprocess.Popen(["git", "--git-dir", repo_bare, "archive", sha], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "-C", work], stdin=archive.stdout)
    if archive.stdout:
        archive.stdout.close()
    archive_code = archive.wait()
    if archive_code != 0 or extract.returncode != 0:
        raise ConfigError("git archive extraction failed")


def registry_login(environ, insecure, log):
    command = ["crane", "auth", "login"]
    if insecure:
        command.append("--insecure")
    command.extend([
        "--username", environ["REGISTRY_USER"],
        "--password-stdin", environ["REGISTRY_ADDRESS"],
    ])
    return run_command(command, log, env=environ, stdin_text=environ["REGISTRY_PASSWORD"])


def ensure_builder(environ, log):
    inspect = ["docker", "buildx", "inspect", environ["BUILDX_BUILDER"]]
    if run_command(inspect, log, env=environ) == 0:
        return 0
    create = [
        "docker", "buildx", "create", "--name", environ["BUILDX_BUILDER"],
        "--driver", "remote", environ["BUILDKIT_ADDRESS"],
    ]
    return run_command(create, log, env=environ)


def build_one(repo, branch, sha, build, context, dockerfile, artifact_dir, log_dir, environ, insecure):
    short_sha = sha[:12]
    image = f"{environ['REGISTRY_ADDRESS']}/{repo}/{build['name']}"
    metadata = {
        "repo": repo,
        "build": build["name"],
        "branch": branch,
        "sha": sha,
        "context": build["context"],
        "dockerfile": build["dockerfile"],
        "image": image,
    }
    log = ExecutionLog(log_dir, "build", metadata)
    image_tar = pathlib.Path(artifact_dir) / f"image-{build['name']}.tar"
    code = run_command([
        "docker", "buildx", "build", "--builder", environ["BUILDX_BUILDER"],
        "--output", f"type=docker,dest={image_tar}", "-f", str(dockerfile), str(context),
    ], log, env=environ)
    if code == 0:
        push = ["crane", "push"]
        if insecure:
            push.append("--insecure")
        code = run_command(push + [str(image_tar), f"{image}:{short_sha}"], log, env=environ)
    if code == 0 and branch == environ["GIT_DEFAULT_BRANCH"]:
        push = ["crane", "push"]
        if insecure:
            push.append("--insecure")
        code = run_command(push + [str(image_tar), f"{image}:latest"], log, env=environ)
    try:
        image_tar.unlink()
    except FileNotFoundError:
        pass
    log.finish(code)
    return code


def execute(repo_bare, branch, sha, repo, environ=None):
    environ = dict(os.environ if environ is None else environ)
    log_root = environ.get("GIT_HOOK_LOGS_ROOT", "/home/git/logs/hooks")
    log_dir = os.path.join(log_root, repo)
    with tempfile.TemporaryDirectory(prefix="git-build-") as temporary_root:
        root = pathlib.Path(temporary_root)
        source = root / "source"
        artifacts = root / "artifacts"
        source.mkdir()
        artifacts.mkdir()
        try:
            materialize_tree(repo_bare, sha, source)
            config_path = source / "repository.yaml"
            if not config_path.is_file():
                return 0
            builds = parse_build_config(config_path.read_text(encoding="utf-8"))
            if not builds:
                return 0
            resolved = [(build, *resolve_build_paths(source, build)) for build in builds]
            insecure = validate_environment(environ)
        except (ConfigError, OSError) as exc:
            return diagnostic_log(log_dir, "build-config", repo, branch, sha, str(exc))

        docker_config = root / "docker-config"
        docker_config.mkdir()
        environ["DOCKER_CONFIG"] = str(docker_config)
        auth_log = ExecutionLog(log_dir, "build-auth", {"repo": repo, "branch": branch, "sha": sha})
        auth_code = registry_login(environ, insecure, auth_log)
        auth_log.finish(auth_code)
        if auth_code != 0:
            return auth_code
        os.remove(auth_log.path)

        infra_log = ExecutionLog(log_dir, "build-infra", {"repo": repo, "branch": branch, "sha": sha})
        infra_code = ensure_builder(environ, infra_log)
        infra_log.finish(infra_code)
        if infra_code != 0:
            return infra_code
        os.remove(infra_log.path)

        failed = False
        for build, context, dockerfile in resolved:
            if build_one(repo, branch, sha, build, context, dockerfile, artifacts, log_dir,
                         environ, insecure) != 0:
                failed = True
        return 1 if failed else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print("usage: build_image.py <bare-repo> <branch> <sha> <repo-name>", file=sys.stderr)
        return 2
    return execute(*argv)


if __name__ == "__main__":
    raise SystemExit(main())
