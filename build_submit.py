#!/usr/bin/env python3
"""Submit one Git build job to the local isolated dispatcher."""

import json
import os
import socket
import sys


SOCKET_PATH = os.environ.get("GIT_BUILD_SOCKET", "/run/git-server/build.sock")
MAX_RESPONSE = 64 * 1024


def submit(branch, sha, repo, socket_path=SOCKET_PATH, timeout=2.0):
    request = json.dumps(
        {"branch": branch, "sha": sha, "repo": repo},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(request)
            response = b""
            while not response.endswith(b"\n") and len(response) <= MAX_RESPONSE:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
    except OSError as exc:
        raise RuntimeError(f"build dispatcher unavailable: {exc}") from exc
    if len(response) > MAX_RESPONSE or not response.endswith(b"\n"):
        raise RuntimeError("invalid response from build dispatcher")
    try:
        result = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid response from build dispatcher") from exc
    if not isinstance(result, dict) or result.get("queued") is not True:
        raise RuntimeError(str(result.get("error", "build was not queued")))
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        print("usage: build_submit.py <branch> <sha> <repo-name>", file=sys.stderr)
        return 2
    try:
        result = submit(*argv)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"queued build job {result['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
