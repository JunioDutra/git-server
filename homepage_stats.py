"""Read-only, private Homepage metrics. No repository code is executed."""

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import re
import stat
import threading
import time


class CollectionError(Exception):
    pass


def read_regular(path, maximum, tail=False):
    """Do not follow log/config symlinks or block on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CollectionError("not a regular file")
        if tail:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - maximum))
        return stream.read(maximum).decode("utf-8", "replace")


class Collector:
    def __init__(self, repos_root, logs_root, hooks_root, *, proc_root="/proc",
                 pidfile="/run/git-build-dispatcher.pid", budget=3.0):
        self.repos = pathlib.Path(repos_root)
        self.logs = pathlib.Path(logs_root)
        self.hooks = pathlib.Path(hooks_root)
        self.proc = pathlib.Path(proc_root)
        self.pidfile = pathlib.Path(pidfile)
        self.budget = budget

    def collect(self):
        deadline = time.monotonic() + self.budget

        def check_budget():
            if time.monotonic() > deadline:
                raise CollectionError("collection budget exceeded")

        # A missing/unreadable root is an error, never an invented zero.
        repositories = []
        with os.scandir(self.repos) as entries:
            for entry in entries:
                check_budget()
                if entry.name.endswith(".git") and entry.is_dir(follow_symlinks=False):
                    repositories.append(pathlib.Path(entry.path))
        total = 0
        for repo in repositories:
            pending = [repo]
            while pending:
                check_budget()
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        check_budget()
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(pathlib.Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
        now = dt.datetime.now(dt.timezone.utc)
        failed = self.failed_builds(now, check_budget)
        running = self.running_jobs()
        return {
            "schemaVersion": 1,
            "status": "ok" if failed is not None and running is not None else "partial",
            "generatedAt": now.isoformat().replace("+00:00", "Z"),
            "repositories": len(repositories),
            "storageBytes": total,
            "buildsRunning": running,
            "buildsFailed24h": failed,
        }

    def failed_builds(self, now, check_budget):
        """Only terminal build records with an explicit UTC completion time count."""
        count, unknown, seen_history = 0, False, False
        cutoff = now - dt.timedelta(hours=24)
        try:
            with os.scandir(self.logs) as entries:
                dirs = [pathlib.Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
            for directory in dirs:
                check_budget()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        check_budget()
                        if not entry.name.endswith(".log") or not entry.is_file(follow_symlinks=False):
                            continue
                        header = read_regular(entry.path, 16384).split(
                            "# hook-log: --- output ---", 1
                        )[0]
                        kind = re.search(
                            r"^# hook-log: type=(build(?:-[a-z-]+)?)$", header, re.M
                        )
                        if not kind:
                            continue
                        seen_history = True
                        # Closed logs older than the window cannot contribute.
                        if entry.stat(follow_symlinks=False).st_mtime < cutoff.timestamp():
                            continue
                        footer = read_regular(entry.path, 4096, tail=True)
                        terminal = re.search(
                            r"(?:^|\n)# hook-log: ended_at=([^\n]+)\n# hook-log: exit=(-?\d+)\n?\Z",
                            footer,
                        )
                        if not terminal:
                            # Open records cannot establish a completed failure.
                            unknown = True
                            continue
                        ended = dt.datetime.fromisoformat(
                            terminal.group(1).replace("Z", "+00:00")
                        )
                        if ended.tzinfo is None:
                            unknown = True
                        elif cutoff <= ended <= now and int(terminal.group(2)) != 0:
                            count += 1
        except (OSError, ValueError):
            return None
        return None if unknown or not seen_history else count

    def running_jobs(self):
        """Count live build workers directly owned by the verified dispatcher PID."""
        try:
            pid = self.pidfile.read_text().strip()
            if not pid.isdecimal():
                return None
            parent = self.proc / pid
            command = (parent / "cmdline").read_bytes().split(b"\0")
            if os.fsencode(str(self.hooks / "build_dispatcher.py")) not in command:
                return None
            # Popen runs in a dispatcher worker thread, not necessarily its main thread.
            children = set()
            for task in (parent / "task").iterdir():
                try:
                    children.update((task / "children").read_text().split())
                except FileNotFoundError:
                    continue
            count = 0
            for child in children:
                if not child.isdecimal():
                    return None
                try:
                    args = (self.proc / child / "cmdline").read_bytes().split(b"\0")
                except FileNotFoundError:
                    continue  # Worker finished during the snapshot.
                if os.fsencode(str(self.hooks / "build_image.py")) in args:
                    count += 1
            return count
        except OSError:
            return None


def error_payload():
    return {
        "schemaVersion": 1,
        "status": "error",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "repositories": None,
        "storageBytes": None,
        "buildsRunning": None,
        "buildsFailed24h": None,
    }


class StatsEndpoint:
    def __init__(self):
        self.lock = threading.Lock()
        self.cached = None
        self.expires = 0
        self.cache_key = None

    def response(self, address, supplied_token, repos, logs, hooks, config_path):
        try:
            config = json.loads(read_regular(config_path, 16384))
            allowed = config["allowedClients"]
            digest = config["tokenSha256"]
            if not isinstance(allowed, list) or not allowed or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid config")
            addresses = {ipaddress.ip_address(value) for value in allowed}
        except (OSError, ValueError, KeyError, TypeError, CollectionError):
            return 503, error_payload()
        # Never trust X-Forwarded-For. Requests from a public proxy stay denied.
        if ipaddress.ip_address(address) not in addresses:
            return 403, {"schemaVersion": 1, "status": "forbidden"}
        if not isinstance(supplied_token, str) or len(supplied_token) > 512:
            return 401, {"schemaVersion": 1, "status": "unauthorized"}
        actual = hashlib.sha256(supplied_token.encode()).hexdigest()
        if not hmac.compare_digest(actual, digest):
            return 401, {"schemaVersion": 1, "status": "unauthorized"}
        key = (repos, logs, hooks, config_path)
        with self.lock:
            if self.cached is not None and self.cache_key == key and time.monotonic() < self.expires:
                return self.cached
            try:
                result = 200, Collector(repos, logs, hooks).collect()
            except (OSError, ValueError, CollectionError):
                result = 503, error_payload()
            self.cached, self.cache_key = result, key
            self.expires = time.monotonic() + (60 if result[0] == 200 else 5)
            return result


endpoint = StatsEndpoint()


def handle(handler, repos, logs, hooks):
    status, payload = endpoint.response(
        handler.client_address[0], handler.headers.get("X-Homepage-Token"),
        repos, logs, hooks,
        os.environ.get("GIT_HOMEPAGE_CONFIG", "/etc/git-http-server/homepage.json"),
    )
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
