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
  GIT_HTTP_HOST   (default: 0.0.0.0)
  GIT_HTTP_PORT   (default: 8080)
"""

import html
import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse

REPOS_ROOT = os.environ.get("GIT_REPOS_ROOT", "/home/git/repos")
HOST = os.environ.get("GIT_HTTP_HOST", "0.0.0.0")
PORT = int(os.environ.get("GIT_HTTP_PORT", "8080"))

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Safe path chars for browsing; reject anything that could escape or inject.
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/ -]+$")
# README candidates, in priority order
README_CANDIDATES = [
    "README.md", "readme.md", "README.MD",
    "Readme.md", "README.markdown", "README.txt", "README",
]


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

README_VIEWER_JS = """
<script>
(function () {
  var raw = document.getElementById('readme-raw');
  if (!raw || typeof marked === 'undefined') return;
  var html = marked.parse(raw.textContent);
  if (typeof DOMPurify !== 'undefined') {
    html = DOMPurify.sanitize(html, {ADD_ATTR: ['target']});
  }
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
  document.getElementById('readme').innerHTML = html;
})();
</script>
"""


def readme_viewer_html(repo_name, branch):
    script = README_VIEWER_JS.replace("REPO_NAME_JS", js_json(repo_name))
    script = script.replace("BRANCH_NAME_JS", js_json(branch))
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
  fetch('/repo/' + encodeURIComponent(name), {method: 'DELETE'})
    .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
    .then(function (res) {
      if (res.ok) { location.href = '/'; }
      else { alert('Error: ' + (res.d.error || 'failed')); }
    })
    .catch(function () { alert('Network error'); });
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
.repo-toolbar{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin:.75rem 0 1rem}
.repo-toolbar select{padding:.35rem .55rem;border:1px solid #d0d7de;border-radius:6px;background:#fff;font-size:.9rem}
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
    for candidate in ("main", "master"):
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
        ["git", "init", "--bare", "-b", "main", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "git init failed"
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
    import shutil
    shutil.rmtree(path)
    return True, path


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
      headers: {{'Content-Type': 'application/json'}},
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
<p class="muted">Empty repository — no commits yet.</p>
<p><button class="btn-danger" onclick="deleteRepo('{js_str(name)}')">Delete repository</button></p>
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

    body = f"""<h1>{html.escape(name)}</h1>
<div class="breadcrumb">📁 {breadcrumb}</div>
{branch_selector(branches, branch)}
<p><a href="{html.escape(with_branch(f'/repo/{name}/log', branch))}">commit log</a> · <a href="/">all repos</a>
 · <button class="btn-danger" onclick="deleteRepo('{js_str(name)}')">Delete repository</button></p>
<table><thead><tr><th>Name</th><th>Type</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""

    readme = get_readme(repo, ref)
    if readme:
        readme_path, readme_content = readme
        body += f"""
<div id="readme-wrapper">
<h2>README <span class="muted">({html.escape(readme_path)})</span></h2>
<pre id="readme-raw" hidden>{html.escape(readme_content)}</pre>
<div id="readme"><p class="muted">Loading README…</p></div>
</div>
{readme_viewer_html(name, branch)}"""

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
        ".html": "html", ".css": "css", ".sql": "sql", ".xml": "xml",
    }.get(ext, "")
    label = f'<span class="muted"> ({lang})</span>' if lang else ""
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


class Handler(BaseHTTPRequestHandler):
    server_version = "GitServer/0.2"

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

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/repo/"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        name = path[len("/repo/"):]
        if "/" in name:
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

        # /repo/<name>/... routes
        if path.startswith("/repo/"):
            parts = path[len("/repo/"):].split("/", 1)
            name = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            repo = repo_path(name)
            if repo is None:
                self._send(404, page("Not found", "<h1>404</h1><p>Unknown repository.</p><p><a href='/'>← back</a></p>"))
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
    os.makedirs(REPOS_ROOT, exist_ok=True)
    print(f"git-server listening on {HOST}:{PORT} (repos root: {REPOS_ROOT})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
