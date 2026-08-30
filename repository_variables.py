#!/usr/bin/env python3
"""Atomic repository-scoped managed variable storage."""

import contextlib
import json
import os
import pathlib
import re
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows-only unit-test fallback
    fcntl = None


DEFAULT_ROOT = "/home/git/repository-env"
VARIABLE_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
REPOSITORY_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_VARIABLES = 128
MAX_VALUE_BYTES = 64 * 1024
MAX_STORAGE_BYTES = MAX_VARIABLES * (MAX_VALUE_BYTES + 256)

RESERVED_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "PWD", "SHELL", "TMPDIR",
    "GIT_REPOS_ROOT", "GIT_HOOKS_ROOT", "GIT_SSH_HOST", "GIT_DEFAULT_BRANCH",
    "GIT_REPOSITORY_ENV_ROOT",
    "DOCKER_CONFIG",
}
RESERVED_PREFIXES = (
    "GIT_SERVER_", "GIT_BUILD_", "GIT_HOOK_", "GIT_HTTP_",
    "REGISTRY_", "BUILDKIT_", "BUILDX_", "DOCKER_",
)


class VariableStoreError(ValueError):
    pass


class VariableStorageError(VariableStoreError):
    pass


def validate_repository_name(name):
    if (not isinstance(name, str) or not REPOSITORY_NAME_RE.fullmatch(name)
            or name in (".", "..")):
        raise VariableStoreError("invalid repository name")
    return name


def validate_variable_name(name):
    if not isinstance(name, str) or not VARIABLE_NAME_RE.fullmatch(name):
        raise VariableStoreError("invalid variable name")
    if name in RESERVED_NAMES or any(name.startswith(prefix) for prefix in RESERVED_PREFIXES):
        raise VariableStoreError(f"reserved variable name: {name}")
    return name


def validate_variable_value(value):
    if not isinstance(value, str):
        raise VariableStoreError("variable values must be strings")
    if "\x00" in value:
        raise VariableStoreError("variable value cannot contain NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VariableStoreError("variable value must be valid UTF-8") from exc
    if len(encoded) > MAX_VALUE_BYTES:
        raise VariableStoreError(f"variable value exceeds {MAX_VALUE_BYTES} bytes")
    return value


class VariableStore:
    def __init__(self, root=None):
        self.root = pathlib.Path(root or os.environ.get("GIT_REPOSITORY_ENV_ROOT", DEFAULT_ROOT))

    def _ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _value_path(self, repository):
        return self.root / f"{validate_repository_name(repository)}.json"

    def _lock_path(self, repository):
        return self.root / f"{validate_repository_name(repository)}.lock"

    @contextlib.contextmanager
    def _locked(self, repository, exclusive):
        self._ensure_root()
        lock_path = self._lock_path(repository)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:  # pragma: no cover - Windows-only unit-test fallback
                os.chmod(lock_path, 0o600)
            with os.fdopen(fd, "a+") as lock_file:
                fd = None
                if fcntl is not None:
                    fcntl.flock(
                        lock_file.fileno(),
                        fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                    )
                yield
        finally:
            if fd is not None:
                os.close(fd)

    def _load_unlocked(self, repository):
        path = self._value_path(repository)
        try:
            if path.stat().st_size > MAX_STORAGE_BYTES:
                raise VariableStorageError("managed variable file is oversized")
            with path.open("r", encoding="utf-8") as source:
                values = json.load(source)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VariableStorageError("managed variable file is malformed") from exc
        if not isinstance(values, dict) or len(values) > MAX_VARIABLES:
            raise VariableStorageError("managed variable file is malformed")
        validated = {}
        try:
            for name, value in values.items():
                validated[validate_variable_name(name)] = validate_variable_value(value)
        except VariableStoreError as exc:
            raise VariableStorageError("managed variable file is malformed") from exc
        return validated

    def load(self, repository):
        with self._locked(repository, exclusive=False):
            return dict(self._load_unlocked(repository))

    def list_configured(self, repository):
        return [
            {"name": name, "configured": True}
            for name in sorted(self.load(repository))
        ]

    def patch(self, repository, upsert, delete):
        if not isinstance(upsert, dict) or not isinstance(delete, list):
            raise VariableStoreError("upsert must be an object and delete must be a list")
        parsed_upsert = {}
        for name, value in upsert.items():
            parsed_upsert[validate_variable_name(name)] = validate_variable_value(value)
        parsed_delete = []
        seen_delete = set()
        for name in delete:
            name = validate_variable_name(name)
            if name in seen_delete:
                raise VariableStoreError(f"duplicate delete variable: {name}")
            seen_delete.add(name)
            parsed_delete.append(name)
        conflict = set(parsed_upsert).intersection(parsed_delete)
        if conflict:
            raise VariableStoreError(f"variable cannot be upserted and deleted: {sorted(conflict)[0]}")

        with self._locked(repository, exclusive=True):
            values = self._load_unlocked(repository)
            values.update(parsed_upsert)
            for name in parsed_delete:
                values.pop(name, None)
            if len(values) > MAX_VARIABLES:
                raise VariableStoreError(f"repository exceeds {MAX_VARIABLES} variables")
            self._write_unlocked(repository, values)
            return [{"name": name, "configured": True} for name in sorted(values)]

    def _write_unlocked(self, repository, values):
        path = self._value_path(repository)
        if not values:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._fsync_root()
            return
        fd, temporary = tempfile.mkstemp(prefix=f".{repository}-", suffix=".tmp", dir=self.root)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:  # pragma: no cover - Windows-only unit-test fallback
                os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                fd = None
                json.dump(values, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._fsync_root()
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def delete_repository(self, repository):
        with self._locked(repository, exclusive=True):
            path = self._value_path(repository)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._fsync_root()

    def _fsync_root(self):
        if os.name == "nt":  # Directory handles cannot be fsynced on Windows.
            return
        directory_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
