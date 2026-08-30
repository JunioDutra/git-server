# Nginx Proxy security boundary

The GitServer HTTP origin does not authenticate users or enforce CSRF itself.
Before enabling managed variables, the external Nginx Proxy and the network
between the proxy and GitServer must enforce this boundary.

## Required proxy behavior

- terminate HTTPS and authenticate every request;
- reject `POST`, `PATCH`, and `DELETE` unless `Origin` is exactly the public
  HTTPS origin and `X-GitServer-CSRF` is exactly `1`;
- do not enable CORS for untrusted origins;
- discard a client-provided `X-Authenticated-User` before optionally setting it
  from the proxy's authenticated identity;
- forward to the GitServer private address only after these checks.

The browser UI sends `X-GitServer-CSRF: 1` on every mutation. Non-browser API
clients must send both that header and the public `Origin` header.

The following maps belong in Nginx's `http` context. Replace the example origin
with the exact public URL; do not use a wildcard or a regular expression that
also accepts attacker-controlled subdomains.

```nginx
map "$request_method:$http_x_gitserver_csrf" $gitserver_csrf_ok {
    default 0;
    ~^(GET|HEAD|OPTIONS): 1;
    ~^(POST|PATCH|DELETE):1$ 1;
}

map "$request_method:$http_origin" $gitserver_origin_ok {
    default 0;
    ~^(GET|HEAD|OPTIONS): 1;
    "POST:https://git.example.internal" 1;
    "PATCH:https://git.example.internal" 1;
    "DELETE:https://git.example.internal" 1;
}
```

Apply the checks inside the authenticated virtual host before `proxy_pass`:

```nginx
if ($gitserver_csrf_ok = 0) { return 403; }
if ($gitserver_origin_ok = 0) { return 403; }

proxy_set_header X-Authenticated-User "";
# With Nginx basic authentication, use this instead when audit identity is needed:
# proxy_set_header X-Authenticated-User $remote_user;
proxy_pass http://GIT_SERVER_PRIVATE_IP:8080;
```

## Required network behavior

The GitServer listener may bind to its private LXC address, but its HTTP port
must accept traffic only from the proxy LXC IP. Enforce that with the Proxmox
firewall or the equivalent host/network firewall. Do not rely on Nginx alone:
direct access to the origin bypasses authentication and CSRF enforcement.

Before deployment is accepted, verify from independent clients that:

1. the private origin is unreachable from every address except the proxy;
2. the public URL rejects an unauthenticated request;
3. an authenticated mutation with a missing/wrong Origin or CSRF header returns
   `403` without being forwarded;
4. an authenticated same-origin mutation with the header succeeds.
