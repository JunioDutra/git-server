#!/usr/bin/env python3
"""Minimal git repo creation HTTP server (stdlib only).

POST /create  -> creates a bare git repository
Body: JSON {"name": "my-repo"}  (or form field name=my-repo)

Config via env:
  GIT_REPOS_ROOT  (default: /home/git/repos)
  GIT_HTTP_HOST   (default: 0.0.0.0)
  GIT_HTTP_PORT   (default: 8080)
"""

import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

REPOS_ROOT = os.environ.get("GIT_REPOS_ROOT", "/home/git/repos")
HOST = os.environ.get("GIT_HTTP_HOST", "0.0.0.0")
PORT = int(os.environ.get("GIT_HTTP_PORT", "8080"))

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def create_bare_repo(name):
    """Create a bare repo at REPOS_ROOT/<name>.git. Returns (ok, message)."""
    if not name:
        return False, "missing name"
    if not NAME_RE.match(name) or name in (".", ".."):
        return False, "invalid name (allowed: letters, digits, . _ -)"
    path = os.path.join(REPOS_ROOT, f"{name}.git")
    if os.path.exists(path):
        return False, f"repo already exists: {name}"
    proc = subprocess.run(
        ["git", "init", "--bare", "-b", "main", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "git init failed"
    return True, path


class Handler(BaseHTTPRequestHandler):
    server_version = "GitCreate/0.1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") != "/create":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        name = None

        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                name = json.loads(raw.decode()).get("name")
            except Exception:
                self._send_json(400, {"ok": False, "error": "invalid JSON body"})
                return
        else:
            params = parse_qs(raw.decode())
            name = (params.get("name") or [None])[0]

        ok, msg = create_bare_repo(name)
        if ok:
            self._send_json(201, {"ok": True, "repo": name, "path": msg})
        else:
            status = 409 if "already exists" in msg else 400
            self._send_json(status, {"ok": False, "error": msg})

    def do_GET(self):
        self._send_json(200, {"ok": True, "service": "git-create", "endpoints": ["POST /create"]})


def main():
    os.makedirs(REPOS_ROOT, exist_ok=True)
    print(f"git-create listening on {HOST}:{PORT} (repos root: {REPOS_ROOT})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()