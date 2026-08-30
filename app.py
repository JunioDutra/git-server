#!/usr/bin/env python3
"""Minimal git repo HTTP server (stdlib only).

POST /create                -> creates a bare git repository
GET  /                      -> HTML index listing all repositories
GET  /repo/<name>/          -> browse repo tree at HEAD (root)
GET  /repo/<name>/tree/<p>  -> browse subdirectory
GET  /repo/<name>/blob/<p>  -> view file content
GET  /repo/<name>/log       -> recent commits (plain list)

Body for /create: JSON {"name": "my-repo"}  (or form field name=my-repo)

Config via env:
  GIT_REPOS_ROOT  (default: /home/git/repos)
  GIT_HTTP_HOST   (required)
  GIT_HTTP_PORT   (required)
  GIT_SSH_HOST    (required)
  GIT_DEFAULT_BRANCH (required)
"""

import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from repository_variables import VariableStore, VariableStoreError, VariableStorageError

REPOS_ROOT = os.environ.get("GIT_REPOS_ROOT", "/home/git/repos")
HOOK_LOGS_ROOT = os.environ.get("GIT_HOOK_LOGS_ROOT", "/home/git/logs/hooks")
HOOK_LOG_RETENTION_DAYS = int(os.environ.get("GIT_HOOK_LOG_RETENTION_DAYS", "30"))
HOOKS_ROOT = os.environ.get("GIT_HOOKS_ROOT", "/home/git/hooks")
HOST = os.environ.get("GIT_HTTP_HOST")
PORT = os.environ.get("GIT_HTTP_PORT")
GIT_SSH_HOST = os.environ.get("GIT_SSH_HOST")
GIT_DEFAULT_BRANCH = os.environ.get("GIT_DEFAULT_BRANCH")
VARIABLE_STORE = VariableStore(os.environ.get("GIT_REPOSITORY_ENV_ROOT"))

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LOG_ID_RE = re.compile(r"^[A-Za-z0-9._-]+\.log$")
# Safe path chars for browsing; reject anything that could escape or inject.
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/ -]+$")
# README candidates, in priority order
README_CANDIDATES = [
    "README.md", "readme.md", "README.MD",
    "Readme.md", "README.markdown", "README.txt", "README",
]
MARKDOWN_EXTENSIONS = {".md", ".markdown"}


def get_readme(repo, ref):
    """Return (readme_path, raw_markdown) for the repo's README, or None."""
    root = git(repo, "ls-tree", ref)
    if root is None:
        return None
    names = []
    for line in root.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta_parts = parts[0].split()
        if len(meta_parts) < 3:
            continue
        if meta_parts[1] == "blob":
            names.append(parts[1].strip())
    for candidate in README_CANDIDATES:
        if candidate in names:
            content = git(repo, "show", f"{ref}:{candidate}")
            if content is not None:
                return candidate, content
    return None


README_LIBS = """<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
"""

MARKDOWN_VIEWER_JS = """
<script>
(function () {
  var raw = document.getElementById(RAW_ID_JS);
  var rendered = document.getElementById(RENDERED_ID_JS);
  var toggle = TOGGLE_ID_JS ? document.getElementById(TOGGLE_ID_JS) : null;
  if (!raw || !rendered) return;

  function showRaw() {
    raw.hidden = false;
    rendered.hidden = true;
    if (toggle) toggle.textContent = 'Ver renderizado';
  }

  function showRendered() {
    raw.hidden = true;
    rendered.hidden = false;
    if (toggle) toggle.textContent = 'Ver raw';
  }

  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    showRaw();
    if (toggle) toggle.disabled = true;
    return;
  }

  var html = marked.parse(raw.textContent);
  html = DOMPurify.sanitize(html, {ADD_ATTR: ['target']});
  var repoName = REPO_NAME_JS;
  var branch = BRANCH_NAME_JS;
  html = html.replace(/href="([^"]+)"/g, function (m, href) {
    if (/^(https?:|mailto:|#)/i.test(href) || href.startsWith('//')) return m;
    if (href.startsWith('/')) return m;
    var hashAt = href.indexOf('#');
    var fragment = hashAt >= 0 ? href.slice(hashAt) : '';
    var path = hashAt >= 0 ? href.slice(0, hashAt) : href;
    var separator = path.indexOf('?') >= 0 ? '&' : '?';
    return 'href="' + location.origin + '/repo/' + encodeURIComponent(repoName) +
      '/blob/' + path + separator + 'ref=' + encodeURIComponent(branch) + fragment + '"';
  });
  rendered.innerHTML = html;
  showRendered();
  if (toggle) {
    toggle.addEventListener('click', function () {
      if (raw.hidden) showRaw();
      else showRendered();
    });
  }
})();
</script>
"""


def markdown_viewer_html(repo_name, branch, raw_id, rendered_id, toggle_id=None):
    """Render Markdown safely, falling back to raw text if CDN libraries fail."""
    script = MARKDOWN_VIEWER_JS.replace("REPO_NAME_JS", js_json(repo_name))
    script = script.replace("BRANCH_NAME_JS", js_json(branch))
    script = script.replace("RAW_ID_JS", js_json(raw_id))
    script = script.replace("RENDERED_ID_JS", js_json(rendered_id))
    script = script.replace("TOGGLE_ID_JS", js_json(toggle_id) if toggle_id else "null")
    return README_LIBS + script


def js_json(value):
    """Encode a string for a JavaScript literal inside an HTML script tag."""
    return (json.dumps(value)
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def js_str(value):
    """Escape a value for safe embedding inside a JS string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")


JS_DELETE_REPO = """
<script>
function deleteRepo(name) {
  if (!confirm('Delete repository "' + name + '"?\\nThis cannot be undone.')) return;
  fetch('/repo/' + encodeURIComponent(name), {method: 'DELETE', headers: {'X-GitServer-CSRF': '1'}})
    .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
    .then(function (res) {
      if (res.ok) { location.href = '/'; }
      else { alert('Error: ' + (res.d.error || 'failed')); }
    })
    .catch(function () { alert('Network error'); });
}
</script>
"""

JS_DELETE_HOOK_LOGS = """
<script>
function deleteHookLog(name, logId) {
  if (!confirm('Delete this hook log?\\nThis cannot be undone.')) return;
  fetch('/repo/' + encodeURIComponent(name) + '/hook-logs/' + encodeURIComponent(logId), {method: 'DELETE', headers: {'X-GitServer-CSRF': '1'}})
    .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
    .then(function (res) { if (res.ok) location.reload(); else alert('Error: ' + (res.d.error || 'failed')); })
    .catch(function () { alert('Network error'); });
}
function clearHookLogs(name) {
  if (!confirm('Delete all hook logs for "' + name + '"?\\nThis cannot be undone.')) return;
  fetch('/repo/' + encodeURIComponent(name) + '/hook-logs', {method: 'DELETE', headers: {'X-GitServer-CSRF': '1'}})
    .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
    .then(function (res) { if (res.ok) location.reload(); else alert('Error: ' + (res.d.error || 'failed')); })
    .catch(function () { alert('Network error'); });
}
</script>
"""

JS_COPY_COMMAND = """
<script>
function copyCommand(button) {
  var command = button.getAttribute('data-command');
  var original = button.textContent;
  function copied() {
    button.textContent = 'Copiado';
    setTimeout(function () { button.textContent = original; }, 1400);
  }
  function fallback() {
    var area = document.createElement('textarea');
    area.value = command;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { if (document.execCommand('copy')) copied(); }
    finally { document.body.removeChild(area); }
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(command).then(copied).catch(fallback);
  } else {
    fallback();
  }
}
</script>
"""

PAGE_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1f2328;line-height:1.5}
h1{font-size:1.5rem;border-bottom:1px solid #d0d7de;padding-bottom:.5rem}
a{color:#0969da;text-decoration:none}a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #d8dee4}
th{background:#f6f8fa;font-weight:600}
.breadcrumb{font-size:.9rem;margin-bottom:1rem}
.dir{font-weight:600}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:1rem;overflow-x:auto;font-size:.85rem}
.muted{color:#57606a;font-size:.85rem}
code{background:#f6f8fa;padding:.1rem .3rem;border-radius:4px;font-size:.85em}
#readme-wrapper{margin-top:1.5rem}
#readme-wrapper h2{border-bottom:1px solid #d0d7de;padding-bottom:.3rem}
#readme{line-height:1.6}
#readme img{max-width:100%}
#readme h1,#readme h2,#readme h3,#readme h4{border-bottom:1px solid #d0d7de;padding-bottom:.3rem;margin-top:1.2rem}
#readme pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:1rem;overflow-x:auto;font-size:.85rem}
#readme code{background:#f6f8fa;padding:.1rem .3rem;border-radius:4px;font-size:.85em}
#readme table{border-collapse:collapse;margin:.5rem 0}
#readme th,#readme td{border:1px solid #d0d7de;padding:.3rem .6rem;text-align:left}
#readme blockquote{border-left:3px solid #d0d7de;margin:.5rem 0;padding:.1rem 1rem;color:#57606a}
.create-box{border:1px solid #d0d7de;border-radius:8px;padding:1rem;margin:1.5rem 0;background:#f6f8fa}
.create-box h2{margin:0 0 .6rem;font-size:1rem}
.create-box form{display:flex;gap:.5rem;flex-wrap:wrap}
.create-box input[type=text]{flex:1;min-width:200px;padding:.45rem .6rem;border:1px solid #d0d7de;border-radius:6px;font-size:.9rem}
.create-box button{padding:.45rem 1rem;background:#1f883d;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:.9rem}
.create-box button:hover{background:#1a7f37}
.create-msg{font-size:.85rem}
.create-msg.ok{color:#1a7f37}
.create-msg.err{color:#cf222e}
.btn-danger{padding:.35rem .8rem;background:#cf222e;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:.8rem}
.btn-danger:hover{background:#b51f2b}
.btn-secondary{padding:.35rem .8rem;background:#57606a;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:.8rem}
.btn-secondary:hover{background:#424a53}.status-ok{color:#1a7f37}.status-failed{color:#cf222e}.status-open{color:#9a6700}
.repo-toolbar{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin:.75rem 0 1rem}
.repo-toolbar select{padding:.35rem .55rem;border:1px solid #d0d7de;border-radius:6px;background:#fff;font-size:.9rem}
.repo-access{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:1rem;margin:1rem 0}
.repo-access h2{font-size:1rem;margin:0 0 .6rem}
.repo-command{display:flex;align-items:center;gap:.5rem;margin:.45rem 0;flex-wrap:wrap}
.repo-command code{flex:1;min-width:0;overflow-wrap:anywhere}
.btn-copy,.btn-toggle{padding:.35rem .8rem;background:#0969da;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:.8rem}
.btn-copy:hover,.btn-toggle:hover{background:#0757b8}.btn-toggle:disabled{background:#8c959f;cursor:not-allowed}
.variables-actions{display:flex;gap:.6rem;margin:1rem 0}.variables-table input[type=password],.variables-table input[type=text]{width:100%;box-sizing:border-box;padding:.35rem .5rem;border:1px solid #d0d7de;border-radius:5px}
.variables-message{min-height:1.5rem}.variables-message.ok{color:#1a7f37}.variables-message.err{color:#cf222e}
.markdown-rendered{line-height:1.6}.markdown-rendered img{max-width:100%}
.markdown-rendered h1,.markdown-rendered h2,.markdown-rendered h3,.markdown-rendered h4{border-bottom:1px solid #d0d7de;padding-bottom:.3rem;margin-top:1.2rem}
.markdown-rendered pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:1rem;overflow-x:auto;font-size:.85rem}
.markdown-rendered code{background:#f6f8fa;padding:.1rem .3rem;border-radius:4px;font-size:.85em}
.markdown-rendered table{border-collapse:collapse;margin:.5rem 0}.markdown-rendered th,.markdown-rendered td{border:1px solid #d0d7de;padding:.3rem .6rem;text-align:left}
.markdown-rendered blockquote{border-left:3px solid #d0d7de;margin:.5rem 0;padding:.1rem 1rem;color:#57606a}
"""


def git(repo_path, *args):
    """Run git against a bare repo. Returns stdout or None on failure."""
    proc = subprocess.run(
        ["git", "--git-dir", repo_path] + list(args),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def list_branches(repo):
    """Return all local branch names from a bare repository."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    if out is None:
        return []
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def select_branch(repo, requested=None):
    """Select an existing branch, falling back when the bare HEAD is dangling."""
    branches = list_branches(repo)
    if requested is not None:
        return (requested if requested in branches else None), branches

    head = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = head.strip() if head else None
    if head in branches:
        return head, branches
    for candidate in dict.fromkeys((GIT_DEFAULT_BRANCH, "main", "master")):
        if candidate is None:
            continue
        if candidate in branches:
            return candidate, branches
    return (branches[0] if branches else None), branches


def with_branch(path, branch):
    """Add the selected branch to an internal browse URL."""
    return f"{path}?{urlencode({'ref': branch})}"


def branch_selector(branches, selected):
    """Render a branch picker which keeps the visitor on the current route."""
    options = "".join(
        f'<option value="{html.escape(branch, quote=True)}"'
        f'{" selected" if branch == selected else ""}>{html.escape(branch)}</option>'
        for branch in branches
    )
    return f"""<div class="repo-toolbar">
<label for="branch-select"><strong>Branch:</strong></label>
<select id="branch-select">{options}</select>
</div>
<script>
(function () {{
  var picker = document.getElementById('branch-select');
  if (!picker) return;
  picker.addEventListener('change', function () {{
    var target = new URL(window.location.href);
    target.searchParams.set('ref', picker.value);
    window.location.href = target.toString();
  }});
}})();
</script>"""


def repo_access_panel(name):
    """Render copyable SSH commands for cloning or adding this repository."""
    repo_url = f"git@{GIT_SSH_HOST}:repos/{name}.git"
    commands = (
        ("Clone", f"git clone {repo_url}"),
        ("Adicionar como origin", f"git remote add origin {repo_url}"),
    )
    rows = "".join(
        f'<div class="repo-command"><span class="muted">{html.escape(label)}</span>'
        f'<code>{html.escape(command)}</code>'
        f'<button class="btn-copy" type="button" data-command="{html.escape(command, quote=True)}" '
        f'onclick="copyCommand(this)">Copiar</button></div>'
        for label, command in commands
    )
    return f"""<div class="repo-access">
<h2>Acesso via SSH</h2>
{rows}
</div>
{JS_COPY_COMMAND}"""


def list_repos():
    """Return sorted list of bare repo names under REPOS_ROOT."""
    if not os.path.isdir(REPOS_ROOT):
        return []
    out = []
    for entry in sorted(os.listdir(REPOS_ROOT)):
        if entry.endswith(".git") and os.path.isdir(os.path.join(REPOS_ROOT, entry)):
            out.append(entry[:-4])
    return out


def repo_path(name):
    """Resolve repo name to a safe bare repo path, or None."""
    if not name or not NAME_RE.match(name) or name in (".", ".."):
        return None
    path = os.path.join(REPOS_ROOT, f"{name}.git")
    return path if os.path.isdir(path) else None


def safe_subpath(sub):
    """Validate a browsing subpath. Returns cleaned path or None."""
    if sub is None:
        return ""
    sub = unquote(sub).lstrip("/")
    if not sub:
        return ""
    if not SAFE_PATH_RE.match(sub) or ".." in sub.split("/"):
        return None
    return sub


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
        ["git", "init", "--bare", "-b", GIT_DEFAULT_BRANCH, path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "git init failed"
    canonical_hook = os.path.join(HOOKS_ROOT, "post-receive")
    if os.path.isfile(canonical_hook):
        try:
            os.symlink(canonical_hook, os.path.join(path, "hooks", "post-receive"))
        except OSError as exc:
            shutil.rmtree(path)
            return False, f"hook install failed: {exc}"
    return True, path


def delete_bare_repo(name):
    """Delete a bare repo at REPOS_ROOT/<name>.git. Returns (ok, message)."""
    if not name:
        return False, "missing name"
    if not NAME_RE.match(name) or name in (".", ".."):
        return False, "invalid name (allowed: letters, digits, . _ -)"
    path = os.path.join(REPOS_ROOT, f"{name}.git")
    if not os.path.isdir(path):
        return False, f"repo not found: {name}"
    try:
        VARIABLE_STORE.delete_repository(name)
    except (OSError, VariableStoreError):
        return False, "managed variable cleanup failed"
    shutil.rmtree(path)
    delete_hook_logs(name)
    return True, path


def hook_log_dir(name):
    """Return the dedicated hook-log directory for a validated repo name."""
    return os.path.join(HOOK_LOGS_ROOT, name)


def legacy_hook_log_paths(name):
    return {
        "legacy-build.log": os.path.join("/home/git/logs/builds", f"{name}.log"),
        "legacy-mirror.log": os.path.join("/home/git/logs/mirrors", f"{name}.log"),
    }


def cleanup_hook_logs(name=None):
    """Delete expired per-execution logs. Legacy aggregate logs are retained."""
    if HOOK_LOG_RETENTION_DAYS < 0:
        return
    cutoff = time.time() - HOOK_LOG_RETENTION_DAYS * 86400
    dirs = [hook_log_dir(name)] if name else []
    if not name and os.path.isdir(HOOK_LOGS_ROOT):
        dirs = [os.path.join(HOOK_LOGS_ROOT, entry) for entry in os.listdir(HOOK_LOGS_ROOT)
                if NAME_RE.match(entry)]
    for directory in dirs:
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            if not LOG_ID_RE.match(entry):
                continue
            path = os.path.join(directory, entry)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except FileNotFoundError:
                pass
        try:
            if not os.listdir(directory):
                os.rmdir(directory)
        except (FileNotFoundError, OSError):
            pass


def hook_log_path(name, log_id):
    """Resolve a log ID to an existing regular file contained in its log root."""
    if log_id in legacy_hook_log_paths(name):
        path = legacy_hook_log_paths(name)[log_id]
        return (path, True) if os.path.isfile(path) else (None, False)
    if not LOG_ID_RE.match(log_id):
        return None, False
    directory = os.path.realpath(hook_log_dir(name))
    path = os.path.realpath(os.path.join(directory, log_id))
    if os.path.commonpath((directory, path)) != directory or not os.path.isfile(path):
        return None, False
    return path, False


def parse_hook_log(path, log_id, legacy=False):
    """Build safe display metadata from a log header and filesystem metadata."""
    try:
        stat = os.stat(path)
        with open(path, "rb") as fh:
            header = fh.read(16384).decode("utf-8", "replace")
            if stat.st_size > 4096:
                fh.seek(-4096, os.SEEK_END)
            else:
                fh.seek(0)
            footer = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    meta = {"id": log_id, "path": path, "legacy": legacy, "size": stat.st_size,
            "mtime": stat.st_mtime, "type": "legacy", "started_at": "", "detail": ""}
    if legacy:
        meta["type"] = "build (legacy)" if log_id == "legacy-build.log" else "mirror (legacy)"
        meta["status"] = "legacy"
        return meta
    for line in header.splitlines():
        if not line.startswith("# hook-log: "):
            continue
        payload = line[len("# hook-log: "):]
        if "=" in payload:
            key, value = payload.split("=", 1)
            meta[key] = value
    meta["type"] = meta.get("type", "unknown")
    meta["started_at"] = meta.get("started_at", "")
    meta["detail"] = meta.get("refs", meta.get("branch", ""))
    exit_match = re.search(r"# hook-log: exit=([-0-9]+)", footer)
    if exit_match:
        meta["status"] = "ok" if exit_match.group(1) == "0" else "failed"
    else:
        meta["status"] = "open"
    return meta


def list_hook_logs(name):
    cleanup_hook_logs(name)
    records = []
    directory = hook_log_dir(name)
    if os.path.isdir(directory):
        for entry in os.listdir(directory):
            if not LOG_ID_RE.match(entry):
                continue
            path, legacy = hook_log_path(name, entry)
            if path:
                record = parse_hook_log(path, entry, legacy)
                if record:
                    records.append(record)
    for log_id, path in legacy_hook_log_paths(name).items():
        if os.path.isfile(path):
            record = parse_hook_log(path, log_id, True)
            if record:
                records.append(record)
    return sorted(records, key=lambda record: record["mtime"], reverse=True)


def delete_hook_logs(name, log_id=None):
    """Delete one log or all logs for a repo, including legacy aggregate files."""
    if log_id is not None:
        path, _legacy = hook_log_path(name, log_id)
        if not path:
            return False, "log not found"
        try:
            os.remove(path)
        except FileNotFoundError:
            return False, "log not found"
        return True, path
    directory = hook_log_dir(name)
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    for path in legacy_hook_log_paths(name).values():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return True, directory


def read_log_tail(path, limit=1024 * 1024):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > limit:
            fh.seek(-limit, os.SEEK_END)
            return fh.read().decode("utf-8", "replace"), True
        return fh.read().decode("utf-8", "replace"), False


def page(title, body):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{PAGE_CSS}</style></head>
<body>{body}</body></html>"""


def index_page():
    repos = list_repos()
    rows = "".join(
        f'<tr><td class="dir"><a href="/repo/{html.escape(r)}/">{html.escape(r)}</a></td>'
        f'<td class="muted">bare</td></tr>'
        for r in repos
    ) or '<tr><td colspan="2" class="muted">No repositories yet.</td></tr>'
    body = f"""<h1>Git Server</h1>
<div class="create-box">
<h2>Create a new repository</h2>
<form id="create-form" autocomplete="off">
<input type="text" id="repo-name" placeholder="repo-name (letters, digits, . _ -)" required>
<button type="submit">Create</button>
</form>
<div id="create-msg" class="create-msg"></div>
</div>
<p class="muted">{len(repos)} repository(ies)</p>
<table><thead><tr><th>Repository</th><th>Type</th></tr></thead><tbody>{rows}</tbody></table>
<p class="muted">API: <code>POST /create</code> with <code>{{"name": "my-repo"}}</code>.</p>
<script>
(function () {{
  var form = document.getElementById('create-form');
  var input = document.getElementById('repo-name');
  var msg = document.getElementById('create-msg');
  form.addEventListener('submit', function (e) {{
    e.preventDefault();
    var name = input.value.trim();
    if (!name) return;
    if (!confirm('Create repository "' + name + '"?')) return;
    msg.textContent = '';
    msg.className = 'create-msg';
    fetch('/create', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-GitServer-CSRF': '1'}},
      body: JSON.stringify({{name: name}})
    }}).then(function (r) {{
      return r.json().then(function (d) {{ return {{ok: r.ok, d: d}}; }});
    }}).then(function (res) {{
      if (res.ok) {{
        msg.textContent = '✓ Repository "' + res.d.repo + '" created.';
        msg.className = 'create-msg ok';
        input.value = '';
        setTimeout(function () {{ location.href = '/repo/' + encodeURIComponent(res.d.repo) + '/'; }}, 800);
      }} else {{
        msg.textContent = '✗ ' + (res.d.error || 'failed');
        msg.className = 'create-msg err';
      }}
    }}).catch(function () {{
      msg.textContent = '✗ network error';
      msg.className = 'create-msg err';
    }});
  }});
}})();
</script>"""
    return page("Git Server", body)


def repo_page(name, repo, sub, branch, branches):
    """Browse a directory (tree) inside the repo."""
    ref = f"refs/heads/{branch}"
    if sub:
        tree_ref = f"{ref}:{sub}"
    else:
        tree_ref = ref
    listing = git(repo, "ls-tree", tree_ref)
    if listing is None:
        if not branches:
            body = f"""<h1>{html.escape(name)}</h1>
{repo_access_panel(name)}
<p class="muted">Empty repository — no commits yet.</p>
<p><a href="/repo/{html.escape(name)}/hook-logs">hook logs</a> · <a href="/repo/{html.escape(name)}/variables">managed variables</a> · <button class="btn-danger" onclick="deleteRepo('{js_str(name)}')">Delete repository</button></p>
<p><a href="/">← back</a></p>"""
            return page(f"{name} — Git Server", body + JS_DELETE_REPO)
        return None

    rows = []
    for line in listing.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta, entry = parts
        meta_parts = meta.split()
        if len(meta_parts) < 3:
            continue
        mode, otype, oid = meta_parts[0], meta_parts[1], meta_parts[2]
        entry_name = entry.strip()
        if otype == "tree":
            href = with_branch(f"/repo/{name}/tree/{sub + '/' if sub else ''}{entry_name}", branch)
            rows.append(f'<tr><td class="dir"><a href="{html.escape(href)}">{html.escape(entry_name)}/</a></td><td class="muted">dir</td></tr>')
        else:
            href = with_branch(f"/repo/{name}/blob/{sub + '/' if sub else ''}{entry_name}", branch)
            rows.append(f'<tr><td><a href="{html.escape(href)}">{html.escape(entry_name)}</a></td><td class="muted">{html.escape(otype)}</td></tr>')

    root_href = with_branch(f"/repo/{name}/", branch)
    crumbs = [f'<a href="{html.escape(root_href)}">{html.escape(name)}</a>']
    acc = ""
    for part in sub.split("/"):
        acc = f"{acc}/{part}" if acc else part
        crumb_href = with_branch(f"/repo/{name}/tree/{acc}", branch)
        crumbs.append(f'<a href="{html.escape(crumb_href)}">{html.escape(part)}</a>')
    breadcrumb = " / ".join(crumbs)

    access_panel = repo_access_panel(name) if not sub else ""
    body = f"""<h1>{html.escape(name)}</h1>
{access_panel}
<div class="breadcrumb">📁 {breadcrumb}</div>
{branch_selector(branches, branch)}
<p><a href="{html.escape(with_branch(f'/repo/{name}/log', branch))}">commit log</a> · <a href="/repo/{html.escape(name)}/hook-logs">hook logs</a> · <a href="/repo/{html.escape(name)}/variables">managed variables</a> · <a href="/">all repos</a>
 · <button class="btn-danger" onclick="deleteRepo('{js_str(name)}')">Delete repository</button></p>
<table><thead><tr><th>Name</th><th>Type</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""

    readme = get_readme(repo, ref) if not sub else None
    if readme:
        readme_path, readme_content = readme
        body += f"""
<div id="readme-wrapper">
<h2>README <span class="muted">({html.escape(readme_path)})</span></h2>
<pre id="readme-raw" hidden>{html.escape(readme_content)}</pre>
<div id="readme" class="markdown-rendered"><p class="muted">Loading README…</p></div>
</div>
{markdown_viewer_html(name, branch, 'readme-raw', 'readme')}"""

    return page(f"{name} — Git Server", body + JS_DELETE_REPO)


def blob_page(name, repo, sub, branch, branches):
    ref = f"refs/heads/{branch}"
    content = git(repo, "show", f"{ref}:{sub}")
    if content is None:
        return None
    ext = os.path.splitext(sub)[1].lower()
    lang = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".go": "go", ".java": "java", ".sh": "bash", ".yml": "yaml",
        ".yaml": "yaml", ".json": "json", ".md": "markdown",
        ".markdown": "markdown",
        ".html": "html", ".css": "css", ".sql": "sql", ".xml": "xml",
    }.get(ext, "")
    label = f'<span class="muted"> ({lang})</span>' if lang else ""
    if ext in MARKDOWN_EXTENSIONS:
        body = f"""<h1>{html.escape(name)} / {html.escape(sub)}</h1>
<div class="breadcrumb"><a href="{html.escape(with_branch(f'/repo/{name}/', branch))}">← {html.escape(name)}</a> · <a href="/">all repos</a></div>
{branch_selector(branches, branch)}
<p class="muted">size: {len(content)} bytes{label}</p>
<p><button id="markdown-toggle" class="btn-toggle" type="button">Ver raw</button></p>
<pre id="markdown-raw" hidden>{html.escape(content)}</pre>
<div id="markdown-rendered" class="markdown-rendered"><p class="muted">Loading Markdown…</p></div>
{markdown_viewer_html(name, branch, 'markdown-raw', 'markdown-rendered', 'markdown-toggle')}"""
    else:
        body = f"""<h1>{html.escape(name)} / {html.escape(sub)}</h1>
<div class="breadcrumb"><a href="{html.escape(with_branch(f'/repo/{name}/', branch))}">← {html.escape(name)}</a> · <a href="/">all repos</a></div>
{branch_selector(branches, branch)}
<p class="muted">size: {len(content)} bytes{label}</p>
<pre>{html.escape(content)}</pre>"""
    return page(f"{sub} — {name}", body)


def log_page(name, repo, branch, branches):
    ref = f"refs/heads/{branch}"
    out = git(repo, "log", "--oneline", "-n", "50", ref)
    if out is None:
        return None
    items = "".join(
        f"<tr><td><code>{html.escape(line.split()[0])}</code></td>"
        f"<td>{html.escape(' '.join(line.split()[1:]))}</td></tr>"
        for line in out.splitlines() if line.strip()
    ) or '<tr><td colspan="2" class="muted">No commits.</td></tr>'
    body = f"""<h1>{html.escape(name)} — commit log</h1>
<p><a href="{html.escape(with_branch(f'/repo/{name}/', branch))}">← files</a> · <a href="/">all repos</a></p>
{branch_selector(branches, branch)}
<table><thead><tr><th>Commit</th><th>Message</th></tr></thead><tbody>{items}</tbody></table>"""
    return page(f"{name} — log", body)


def variables_page(name):
    variables = VARIABLE_STORE.list_configured(name)
    rows = []
    for item in variables:
        variable_name = html.escape(item["name"], quote=True)
        rows.append(f"""<tr class="existing-variable" data-name="{variable_name}">
<td><code>{variable_name}</code></td>
<td><input class="variable-value" type="password" value="" placeholder="***" autocomplete="new-password" disabled></td>
<td><label><input class="variable-replace" type="checkbox"> Replace</label></td>
<td><label><input class="variable-delete" type="checkbox"> Delete</label></td>
</tr>""")
    existing_rows = "".join(rows) or '<tr id="empty-variables"><td colspan="4" class="muted">No managed variables configured.</td></tr>'
    body = f"""<h1>{html.escape(name)} — managed variables</h1>
<p><a href="/repo/{html.escape(name)}/">← repository</a></p>
<p class="muted">Values are never returned. An untouched field preserves its configured value.</p>
<table class="variables-table"><thead><tr><th>Name</th><th>New value</th><th>Replace</th><th>Delete</th></tr></thead>
<tbody id="variable-rows">{existing_rows}</tbody></table>
<div class="variables-actions"><button id="add-variable" class="btn-secondary" type="button">Add variable</button>
<button id="save-variables" type="button">Save changes</button></div>
<p id="variables-message" class="variables-message"></p>
<script>
(function () {{
  var repo = {js_json(name)};
  var rows = document.getElementById('variable-rows');
  var message = document.getElementById('variables-message');
  function bindExisting(row) {{
    var replace = row.querySelector('.variable-replace');
    var value = row.querySelector('.variable-value');
    var remove = row.querySelector('.variable-delete');
    replace.addEventListener('change', function () {{
      value.disabled = !replace.checked;
      if (replace.checked) value.focus();
    }});
    remove.addEventListener('change', function () {{
      replace.disabled = remove.checked;
      value.disabled = remove.checked || !replace.checked;
    }});
  }}
  Array.prototype.forEach.call(rows.querySelectorAll('.existing-variable'), bindExisting);
  document.getElementById('add-variable').addEventListener('click', function () {{
    var empty = document.getElementById('empty-variables');
    if (empty) empty.remove();
    var row = document.createElement('tr');
    row.className = 'new-variable';
    row.innerHTML = '<td><input class="new-name" type="text" placeholder="VARIABLE_NAME" autocomplete="off"></td>' +
      '<td><input class="new-value" type="password" autocomplete="new-password"></td>' +
      '<td class="muted">New</td><td><button type="button" class="btn-danger remove-new">Remove</button></td>';
    row.querySelector('.remove-new').addEventListener('click', function () {{ row.remove(); }});
    rows.appendChild(row);
    row.querySelector('.new-name').focus();
  }});
  document.getElementById('save-variables').addEventListener('click', function () {{
    var upsert = {{}};
    var remove = [];
    Array.prototype.forEach.call(rows.querySelectorAll('.existing-variable'), function (row) {{
      var name = row.getAttribute('data-name');
      if (row.querySelector('.variable-delete').checked) remove.push(name);
      else if (row.querySelector('.variable-replace').checked) upsert[name] = row.querySelector('.variable-value').value;
    }});
    Array.prototype.forEach.call(rows.querySelectorAll('.new-variable'), function (row) {{
      var name = row.querySelector('.new-name').value.trim();
      if (name) upsert[name] = row.querySelector('.new-value').value;
    }});
    message.textContent = '';
    message.className = 'variables-message';
    fetch('/api/repo/' + encodeURIComponent(repo) + '/variables', {{
      method: 'PATCH',
      headers: {{'Content-Type': 'application/json', 'X-GitServer-CSRF': '1'}},
      body: JSON.stringify({{upsert: upsert, delete: remove}})
    }}).then(function (response) {{
      return response.json().then(function (data) {{ return {{ok: response.ok, data: data}}; }});
    }}).then(function (result) {{
      if (!result.ok) throw new Error(result.data.error || 'failed');
      message.textContent = '✓ Variables saved without revealing their values.';
      message.className = 'variables-message ok';
      setTimeout(function () {{ location.reload(); }}, 700);
    }}).catch(function (error) {{
      message.textContent = '✗ ' + error.message;
      message.className = 'variables-message err';
    }});
  }});
}})();
</script>"""
    return page(f"{name} — managed variables", body)


def hook_logs_page(name):
    records = list_hook_logs(name)
    current = [record for record in records if not record["legacy"]]
    legacy = [record for record in records if record["legacy"]]

    def render_rows(items):
        rows = []
        for record in items:
            status = record["status"]
            status_label = {"ok": "completed", "failed": "failed", "open": "running/incomplete",
                            "legacy": "legacy"}.get(status, status)
            status_class = {"ok": "status-ok", "failed": "status-failed", "open": "status-open"}.get(status, "muted")
            detail = record.get("detail") or "—"
            execution_name = record.get("task") or record.get("build") or "—"
            image = record.get("image") or "—"
            started = record.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["mtime"]))
            href = f"/repo/{name}/hook-logs/{record['id']}"
            rows.append(
                f'<tr><td><a href="{html.escape(href, quote=True)}">{html.escape(started)}</a></td>'
                f'<td>{html.escape(record["type"])}</td><td><code>{html.escape(execution_name)}</code></td>'
                f'<td><code>{html.escape(detail)}</code></td><td><code>{html.escape(image)}</code></td>'
                f'<td>{record["size"]:,} bytes</td><td class="{status_class}">{status_label}</td>'
                f'<td><button class="btn-danger" onclick="deleteHookLog(\'{js_str(name)}\', \'{js_str(record["id"])}\')">Delete</button></td></tr>'
            )
        return "".join(rows)

    current_rows = render_rows(current) or '<tr><td colspan="8" class="muted">No hook executions yet.</td></tr>'
    legacy_section = ""
    if legacy:
        legacy_section = f"""<h2>Legacy aggregate logs</h2>
<p class="muted">These files contain multiple executions recorded before per-execution logging was introduced.</p>
<table><thead><tr><th>Started</th><th>Type</th><th>Build / task</th><th>Branch / refs</th><th>Image</th><th>Size</th><th>Status</th><th></th></tr></thead><tbody>{render_rows(legacy)}</tbody></table>"""
    body = f"""<h1>{html.escape(name)} — hook logs</h1>
<p><a href="/repo/{html.escape(name)}/">← files</a> · <a href="/">all repos</a></p>
<p><button class="btn-danger" onclick="clearHookLogs('{js_str(name)}')">Delete all logs</button></p>
<h2>Hook executions</h2>
<table><thead><tr><th>Started</th><th>Type</th><th>Build / task</th><th>Branch / refs</th><th>Image</th><th>Size</th><th>Status</th><th></th></tr></thead><tbody>{current_rows}</tbody></table>
{legacy_section}"""
    return page(f"{name} — hook logs", body + JS_DELETE_HOOK_LOGS)


def hook_log_detail_page(name, log_id):
    path, _legacy = hook_log_path(name, log_id)
    if not path:
        return None
    try:
        content, truncated = read_log_tail(path)
    except FileNotFoundError:
        return None
    notice = '<p class="muted">Showing the last 1 MiB. Download the raw file for the complete log.</p>' if truncated else ""
    raw_href = f"/repo/{name}/hook-logs/{log_id}/raw"
    body = f"""<h1>{html.escape(name)} — hook log</h1>
<p><a href="/repo/{html.escape(name)}/hook-logs">← all hook logs</a> · <a href="{html.escape(raw_href, quote=True)}">download raw</a></p>
{notice}<pre>{html.escape(content)}</pre>"""
    return page(f"{name} — hook log", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "GitServer/0.3"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload), "application/json")

    def _read_json(self, maximum=20 * 1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 1 or length > maximum:
            raise ValueError("invalid request size")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        return payload

    def _send_file(self, path, filename):
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            with open(path, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
        except FileNotFoundError:
            self._send(404, page("Not found", "<p>Log not found.</p>"))

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

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/")
        match = re.fullmatch(r"/api/repo/([A-Za-z0-9._-]+)/variables", path)
        if not match:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        name = match.group(1)
        if repo_path(name) is None:
            self._send_json(404, {"ok": False, "error": "repo not found"})
            return
        try:
            payload = self._read_json()
            if not isinstance(payload, dict) or set(payload) != {"upsert", "delete"}:
                raise ValueError("invalid variable patch")
            variables = VARIABLE_STORE.patch(name, payload["upsert"], payload["delete"])
        except VariableStorageError:
            self._send_json(500, {"ok": False, "error": "managed variable storage unavailable"})
            return
        except (ValueError, VariableStoreError):
            self._send_json(400, {"ok": False, "error": "invalid variable patch"})
            return
        except OSError:
            self._send_json(500, {"ok": False, "error": "managed variable storage unavailable"})
            return
        self._send_json(200, {"variables": variables})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/repo/"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        parts = path[len("/repo/"):].split("/")
        name = parts[0]
        if not name or not NAME_RE.match(name):
            self._send_json(400, {"ok": False, "error": "invalid repo name"})
            return
        if len(parts) >= 2 and parts[1] == "hook-logs":
            if repo_path(name) is None:
                self._send_json(404, {"ok": False, "error": "repo not found"})
                return
            if len(parts) == 2:
                ok, msg = delete_hook_logs(name)
            elif len(parts) == 3:
                ok, msg = delete_hook_logs(name, parts[2])
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            if ok:
                self._send_json(200, {"ok": True, "repo": name, "path": msg})
            else:
                self._send_json(404, {"ok": False, "error": msg})
            return
        if len(parts) != 1:
            self._send_json(400, {"ok": False, "error": "invalid repo name"})
            return
        ok, msg = delete_bare_repo(name)
        if ok:
            self._send_json(200, {"ok": True, "repo": name, "path": msg})
        else:
            status = 404 if "not found" in msg else 400
            self._send_json(status, {"ok": False, "error": msg})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        requested_branch = (parse_qs(parsed.query).get("ref") or [None])[0]

        if path == "/":
            self._send(200, index_page())
            return

        api_match = re.fullmatch(r"/api/repo/([A-Za-z0-9._-]+)/variables", path)
        if api_match:
            name = api_match.group(1)
            if repo_path(name) is None:
                self._send_json(404, {"ok": False, "error": "repo not found"})
                return
            try:
                variables = VARIABLE_STORE.list_configured(name)
            except (OSError, VariableStoreError):
                self._send_json(500, {"ok": False, "error": "managed variable storage unavailable"})
                return
            self._send_json(200, {"variables": variables})
            return

        # /repo/<name>/... routes
        if path.startswith("/repo/"):
            parts = path[len("/repo/"):].split("/", 1)
            name = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            repo = repo_path(name)
            if repo is None:
                self._send(404, page("Not found", "<h1>404</h1><p>Unknown repository.</p><p><a href='/'>← back</a></p>"))
                return
            if rest == "hook-logs":
                self._send(200, hook_logs_page(name))
                return
            if rest == "variables":
                try:
                    output = variables_page(name)
                except (OSError, VariableStoreError):
                    self._send(500, page("Error", "<p>Managed variable storage unavailable.</p>"))
                    return
                self._send(200, output)
                return
            if rest.startswith("hook-logs/"):
                log_rest = rest[len("hook-logs/"):]
                if log_rest.endswith("/raw"):
                    log_id = log_rest[:-len("/raw")]
                    log_path, _legacy = hook_log_path(name, log_id)
                    if not log_path:
                        self._send(404, page("Not found", "<p>Log not found.</p>"))
                    else:
                        self._send_file(log_path, log_id)
                    return
                if "/" in log_rest:
                    self._send(404, page("Not found", "<p>Unknown route.</p>"))
                    return
                out = hook_log_detail_page(name, log_rest)
                self._send(200 if out else 404, out or page("Not found", "<p>Log not found.</p>"))
                return
            branch, branches = select_branch(repo, requested_branch)
            if requested_branch is not None and branch is None:
                self._send(404, page("Branch not found", "<h1>404</h1><p>Unknown branch.</p>"))
                return
            if rest == "" or rest == "tree":
                self._send(200, repo_page(name, repo, "", branch or "", branches))
                return
            if rest == "log":
                out = log_page(name, repo, branch, branches) if branch else None
                self._send(200 if out else 404, out or page("Error", "<p>Cannot read log.</p>"))
                return
            if rest.startswith("tree/"):
                sub = safe_subpath(rest[len("tree/"):])
                if sub is None:
                    self._send(400, page("Bad path", "<p>Invalid path.</p>"))
                    return
                out = repo_page(name, repo, sub, branch, branches) if branch else None
                self._send(200 if out else 404, out or page("Not found", "<p>Path not found.</p>"))
                return
            if rest.startswith("blob/"):
                sub = safe_subpath(rest[len("blob/"):])
                if sub is None:
                    self._send(400, page("Bad path", "<p>Invalid path.</p>"))
                    return
                out = blob_page(name, repo, sub, branch, branches) if branch else None
                self._send(200 if out else 404, out or page("Not found", "<p>File not found.</p>"))
                return
            self._send(404, page("Not found", "<p>Unknown route.</p>"))

        self._send(404, page("Not found", "<p>Unknown route.</p>"))


def main():
    missing = [name for name, value in (("GIT_HTTP_HOST", HOST), ("GIT_HTTP_PORT", PORT),
                                         ("GIT_SSH_HOST", GIT_SSH_HOST),
                                         ("GIT_DEFAULT_BRANCH", GIT_DEFAULT_BRANCH)) if not value]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")
    os.makedirs(REPOS_ROOT, exist_ok=True)
    os.makedirs(HOOK_LOGS_ROOT, exist_ok=True)
    cleanup_hook_logs()

    def maintain_hook_logs():
        while True:
            time.sleep(3600)
            cleanup_hook_logs()

    threading.Thread(target=maintain_hook_logs, daemon=True).start()
    print(f"git-server listening on {HOST}:{PORT} (repos root: {REPOS_ROOT})")
    ThreadingHTTPServer((HOST, int(PORT)), Handler).serve_forever()


if __name__ == "__main__":
    main()
