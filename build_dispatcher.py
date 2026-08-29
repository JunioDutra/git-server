#!/usr/bin/env python3
"""Isolated durable build queue served over a local Unix socket."""

import json
import os
import pathlib
import queue
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading

from build_image import ConfigError, diagnostic_log, validate_branch_name


REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
REQUEST_KEYS = {"repo", "branch", "sha"}
MAX_REQUEST = 64 * 1024


class JobError(ValueError):
    pass


def positive_int(environ, name, default, maximum):
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise JobError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise JobError(f"{name} must be between 1 and {maximum}")
    return value


def validate_job(payload, repos_root):
    if not isinstance(payload, dict) or set(payload) != REQUEST_KEYS:
        raise JobError("job must contain exactly repo, branch, and sha")
    repo = payload["repo"]
    branch = payload["branch"]
    sha = payload["sha"]
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise JobError("invalid repository name")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise JobError("invalid commit SHA")
    try:
        branch = validate_branch_name(branch, "branch")
    except ConfigError as exc:
        raise JobError(str(exc)) from exc
    root = pathlib.Path(repos_root).resolve()
    repo_path = (root / f"{repo}.git").resolve()
    try:
        repo_path.relative_to(root)
    except ValueError as exc:
        raise JobError("repository escapes configured root") from exc
    if not repo_path.is_dir():
        raise JobError("repository not found")
    return {"repo": repo, "branch": branch, "sha": sha, "repo_path": str(repo_path)}


def atomic_write_job(job, queue_root):
    root = pathlib.Path(queue_root)
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="build-", suffix=".tmp", dir=root)
    final = pathlib.Path(temporary).with_suffix(".job")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({key: job[key] for key in ("repo", "branch", "sha")}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return final


class BuildQueue:
    def __init__(self, environ=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.repos_root = self.environ.get("GIT_REPOS_ROOT", "/home/git/repos")
        self.queue_root = self.environ.get("GIT_BUILD_QUEUE_ROOT", "/home/git/build-queue")
        self.log_root = self.environ.get("GIT_HOOK_LOGS_ROOT", "/home/git/logs/hooks")
        self.worker = self.environ.get("GIT_BUILD_WORKER", "/home/git/hooks/build_image.py")
        self.capacity = positive_int(self.environ, "GIT_BUILD_QUEUE_SIZE", 100, 10000)
        self.jobs = queue.Queue()
        self.lock = threading.Lock()

    def recover(self):
        pathlib.Path(self.queue_root).mkdir(parents=True, exist_ok=True)
        for path in sorted(pathlib.Path(self.queue_root).glob("*.job")):
            self.jobs.put(path)

    def submit(self, payload):
        job = validate_job(payload, self.repos_root)
        with self.lock:
            pending = len(list(pathlib.Path(self.queue_root).glob("*.job")))
            if pending >= self.capacity:
                raise JobError("build queue is full")
            path = atomic_write_job(job, self.queue_root)
            self.jobs.put(path)
        return path.stem

    def run_job(self, path):
        job = None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            job = validate_job(payload, self.repos_root)
            subprocess.run(
                [self.worker, job["repo_path"], job["branch"], job["sha"], job["repo"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.environ,
                check=False,
            )
        except (OSError, ValueError) as exc:
            if job:
                log_dir = os.path.join(self.log_root, job["repo"])
                diagnostic_log(
                    log_dir, "build-dispatcher", job["repo"], job["branch"], job["sha"],
                    f"dispatcher failed to execute job: {exc}",
                )
            else:
                print(f"cannot recover build job {path.name}: {exc}", file=sys.stderr)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def worker_loop(self):
        while True:
            path = self.jobs.get()
            try:
                self.run_job(path)
            finally:
                self.jobs.task_done()


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                raise JobError("invalid or oversized request")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JobError("request must be valid JSON") from exc
            job_id = self.server.build_queue.submit(payload)
            response = {"queued": True, "id": job_id}
        except (JobError, OSError) as exc:
            response = {"queued": False, "error": str(exc)}
        self.wfile.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")


if hasattr(socketserver, "UnixStreamServer"):
    class UnixBuildServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def __init__(self, socket_path, build_queue):
            self.build_queue = build_queue
            super().__init__(socket_path, RequestHandler)
else:
    class UnixBuildServer:
        def __init__(self, socket_path, build_queue):
            raise RuntimeError("Unix domain sockets are not supported on this platform")


def serve(environ=None):
    environ = dict(os.environ if environ is None else environ)
    socket_path = pathlib.Path(
        environ.get("GIT_BUILD_SOCKET", "/run/git-server/build.sock")
    )
    workers = positive_int(environ, "GIT_BUILD_WORKERS", 1, 16)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if socket_path.exists() or socket_path.is_socket():
            socket_path.unlink()
    except OSError as exc:
        raise RuntimeError(f"cannot remove stale build socket: {exc}") from exc

    build_queue = BuildQueue(environ)
    build_queue.recover()
    for index in range(workers):
        threading.Thread(
            target=build_queue.worker_loop,
            name=f"build-worker-{index + 1}",
            daemon=True,
        ).start()

    try:
        with UnixBuildServer(str(socket_path), build_queue) as server:
            os.chmod(socket_path, 0o660)
            server.serve_forever()
    finally:
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    serve()
