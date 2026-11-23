# Hardening the HTTP surface

This API is consumed by scripts and by a browser dashboard, and the browser is
the dangerous consumer: it attaches ambient authority, renders whatever comes
back, and follows whatever a page tells it to. The protections in
`anvil_server.http_security` are chosen so that none of that can be turned
against a project's recordings.

## Nothing is a document

Every response — success, problem, 404 and a rejected oversized body alike —
carries:

| Header | Value |
| --- | --- |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; sandbox` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `no-referrer` |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Permissions-Policy` | hardware and payment features disabled |
| `Cache-Control` | `no-store` |

`default-src 'none'` is the whole point. A JSON API has no legitimate need to
load or execute anything, so even if a recorded value were ever echoed into a
response, a browser has no way to run it, fetch for it or frame the page that
holds it. `no-store` keeps one project's recording out of a shared cache and
out of browser history.

A route cannot weaken these: the middleware is outermost and its headers
replace a route's own. A route's `Vary` is combined rather than discarded,
because a route that varies on something else is still right about that.

## Cross-origin access is deny by default

`ANVIL_ALLOWED_ORIGINS` names the origins allowed to call the API. An
unconfigured deployment allows **none**, and sends no CORS headers at all —
the correct answer rather than an omission, because a deployment that never
configured a dashboard never accidentally exposed one.

The allowlist is exact. `*` is not expanded, `null` is not honoured, and an
entry that is not a bare `scheme://host[:port]` is dropped rather than guessed
at: a half-understood origin is the one that matches too much.
`Vary: Origin` travels with every response, including refusals, so a cache
cannot serve one origin the decision made for another.

`Access-Control-Allow-Credentials` is never sent. This API authenticates with
a bearer token and nothing else, so allowing credentials could only enable
cookies — the ambient authority it refuses to have.

A preflight is answered only for an allowed origin, an accepted method
(`GET`, `HEAD`, `POST`, `DELETE`, `OPTIONS`) and the published request
headers (`Authorization`, `Content-Type`, `Idempotency-Key`, `If-None-Match`,
`Last-Event-ID`). Anything else is a `403` with a problem body rather than a
silent `204`, because a refused preflight that looks successful is a
deployment mistake nobody notices until later.

## CSRF is removed, not mitigated

There is no synchronizer token because there is nothing to synchronize: a
request is authenticated by its `Authorization` header, cookies carry no
authority, and a cross-site form therefore has no credential to spend.

On top of that, a state-changing request carrying a browser `Origin` this
deployment does not allow is refused with `403 cross_origin_rejected` before
it reaches a route. The missing CORS header would already stop the attacker
*reading* the reply; refusing early is the difference between leaking nothing
and performing the side effect anyway. Safe methods still reach their route,
because they change nothing and the absent header is what protects them.
Requests with no `Origin` at all — scripts, CI, server-to-server — are
unaffected.

## Request size

Bodies are capped before they are buffered: 4 MiB by default, 12 MiB for
artifact upload and a bound derived from `MAX_BUNDLE` for verified imports.
A declared `Content-Length` over the cap is refused immediately, and the
streamed body is counted as it arrives so a lying or absent length cannot
smuggle a larger one. The refusal is `413 payload_too_large` with the same
security headers as any other response.

## Errors, cancellation and cleanup

The middleware adds headers and can refuse a request before it reaches a
route. It opens no connection, file or task, so there is nothing to release.
A refusal is a complete response rather than a half-written one, and a client
that disconnects mid-request is handled exactly as it is for any route.
