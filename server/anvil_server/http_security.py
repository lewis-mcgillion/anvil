# Copyright 2026 Anvil contributors
# SPDX-License-Identifier: Apache-2.0
"""Browser-facing hardening for the REST surface (ANVIL-068).

This API is consumed by scripts and by a browser dashboard, and the browser
is the dangerous consumer: it will attach ambient authority, render whatever
comes back and follow whatever a page tells it to. The protections here are
chosen so that none of that can be turned against a project's recordings.

* **Nothing is a document.** Every response carries a content security policy
  of `default-src 'none'` and `frame-ancestors 'none'`, plus `nosniff`. An
  endpoint returns JSON; if an attacker ever gets a recorded value echoed into
  a response, a browser still has no way to execute it, load anything for it
  or frame the page that holds it.
* **Cross-origin is deny by default.** No origin is allowed unless an operator
  names it exactly. There is no wildcard, no pattern, no `null`, and
  credentials are never allowed, because this API authenticates with a bearer
  token and nothing else.
* **CSRF is removed rather than mitigated.** A request is authenticated only
  by its `Authorization` header. Cookies carry no authority at all, so there
  is no ambient credential for a cross-site form to spend, and no token to
  synchronize. A state-changing request that arrives with a browser `Origin`
  this deployment does not know is refused outright, so an attacker's page
  cannot even provoke the side effect.
* **Recordings are not cached.** Responses are `no-store`: a shared cache or
  a browser history entry holding another project's recording is a disclosure
  nobody asked for.

Errors, cancellation and cleanup: the middleware adds headers and can refuse
a request before it reaches a route. It opens nothing, so there is nothing to
release; a refusal is a complete response, and a client disconnecting mid-
request is handled by the server as it would be for any route.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "SECURITY_HEADERS",
    "ALLOWED_METHODS",
    "ALLOWED_REQUEST_HEADERS",
    "SAFE_METHODS",
    "CorsPolicy",
    "SecurityMiddleware",
    "parse_origins",
]

#: Sent on every response, including errors. `default-src 'none'` is the whole
#: point: a JSON API has no legitimate need to load or execute anything.
SECURITY_HEADERS: dict[str, str] = {
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'; sandbox"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "accelerometer=(), camera=(), geolocation=(), "
                          "microphone=(), payment=(), usb=()",
    "cache-control": "no-store",
}

#: Methods the API uses. Anything else is refused at the preflight.
ALLOWED_METHODS = ("GET", "HEAD", "POST", "DELETE", "OPTIONS")

#: Request headers a browser may send cross-origin. Deliberately short.
ALLOWED_REQUEST_HEADERS = ("authorization", "content-type", "idempotency-key",
                           "if-none-match", "last-event-id")

#: Methods that cannot change state, and so cannot be the target of CSRF.
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

_EXPOSED_HEADERS = ("x-request-id", "etag", "retry-after")
_PREFLIGHT_MAX_AGE = "600"


def parse_origins(value: str | None) -> tuple[str, ...]:
    """Parse an allowlist, dropping anything that is not a usable origin.

    A wildcard is not expanded and `null` is not honoured: both of them mean
    "any page", and an API that serves recordings has no such caller. An entry
    that is not an exact `scheme://host[:port]` is dropped rather than
    guessed at, because a half-understood origin is the one that matches too
    much.
    """
    if not value:
        return ()
    origins: list[str] = []
    for raw in value.replace(",", " ").split():
        candidate = raw.strip().rstrip("/")
        if candidate in ("*", "null", ""):
            continue
        if not candidate.startswith(("http://", "https://")):
            continue
        rest = candidate.split("://", 1)[1]
        if not rest or "/" in rest or "?" in rest or "#" in rest:
            continue
        if candidate not in origins:
            origins.append(candidate)
    return tuple(origins)


@dataclass(frozen=True)
class CorsPolicy:
    """Which browser origins may talk to this deployment.

    Empty by default. An empty policy sends no CORS headers at all, which is
    the correct answer rather than an omission: the browser then refuses the
    response, and a deployment that never configured a dashboard never
    accidentally exposed one.
    """

    origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> CorsPolicy:
        source = environ if environ is not None else os.environ
        return cls(origins=parse_origins(source.get("ANVIL_ALLOWED_ORIGINS")))

    def allows(self, origin: str | None) -> bool:
        if not origin:
            return False
        return origin.rstrip("/") in self.origins

    def headers(self, origin: str | None) -> list[tuple[bytes, bytes]]:
        """CORS headers for an allowed origin, or none at all.

        `Vary: Origin` is always sent, including when the origin is refused,
        so a cache cannot serve one origin the decision made for another.
        """
        headers: list[tuple[bytes, bytes]] = [(b"vary", b"Origin")]
        if not self.allows(origin):
            return headers
        assert origin is not None
        headers.append((b"access-control-allow-origin", origin.rstrip("/").encode()))
        headers.append((b"access-control-expose-headers",
                        ", ".join(_EXPOSED_HEADERS).encode()))
        # No access-control-allow-credentials, ever: a bearer token is sent
        # explicitly, so allowing credentials would only enable cookies, which
        # is the ambient authority this API refuses to have.
        return headers


class SecurityMiddleware:
    """Apply the headers, the CORS policy and the cross-origin refusal."""

    def __init__(self, application: ASGIApp, policy: CorsPolicy | None = None):
        self.application = application
        self._policy = policy

    @property
    def policy(self) -> CorsPolicy:
        # Resolved lazily so a deployment's configuration is read at request
        # time rather than frozen at import time by whatever imported first.
        return self._policy if self._policy is not None else CorsPolicy.from_environment()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.application(scope, receive, send)

        policy = self.policy
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        origin = headers.get(b"origin", b"").decode("latin-1") or None
        method = scope.get("method", "GET").upper()

        if method == "OPTIONS" and b"access-control-request-method" in headers:
            return await self._preflight(scope, send, policy, origin, headers)

        if origin and not policy.allows(origin) and method not in SAFE_METHODS:
            # A cross-site page can send this request even though it will not
            # be allowed to read the reply, so refusing it before the route is
            # the difference between leaking nothing and performing the side
            # effect anyway.
            return await self._refuse(
                scope, send, policy, origin, 403, "cross_origin_rejected",
                "Cross-origin state-changing requests are not accepted from this origin",
            )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = _merge(message.get("headers", []),
                                            policy.headers(origin))
            await send(message)

        await self.application(scope, receive, send_wrapper)

    async def _preflight(self, scope: Scope, send: Send, policy: CorsPolicy,
                         origin: str | None, headers: dict[bytes, bytes]) -> None:
        """Answer a preflight, allowing only what this API actually accepts."""
        if not policy.allows(origin):
            # 403 rather than a silent 204: a refused preflight that looks
            # successful is a deployment mistake nobody notices until later.
            return await self._refuse(
                scope, send, policy, origin, 403, "origin_not_allowed",
                "This origin is not allowed to call the API",
            )
        requested = headers.get(b"access-control-request-method", b"").decode("latin-1").upper()
        if requested not in ALLOWED_METHODS:
            return await self._refuse(
                scope, send, policy, origin, 403, "method_not_allowed_cross_origin",
                f"Method {requested or 'unknown'} is not accepted cross-origin",
            )
        asked = [item.strip().lower()
                 for item in headers.get(b"access-control-request-headers", b"")
                 .decode("latin-1").split(",") if item.strip()]
        unknown = [item for item in asked if item not in ALLOWED_REQUEST_HEADERS]
        if unknown:
            return await self._refuse(
                scope, send, policy, origin, 403, "header_not_allowed_cross_origin",
                f"Header(s) not accepted cross-origin: {', '.join(sorted(unknown))}",
            )
        response_headers = _merge(
            [(key.encode(), value.encode()) for key, value in SECURITY_HEADERS.items()],
            policy.headers(origin)
            + [(b"access-control-allow-methods", ", ".join(ALLOWED_METHODS).encode()),
               (b"access-control-allow-headers", ", ".join(ALLOWED_REQUEST_HEADERS).encode()),
               (b"access-control-max-age", _PREFLIGHT_MAX_AGE.encode())],
        )
        await send({"type": "http.response.start", "status": 204,
                    "headers": response_headers})
        await send({"type": "http.response.body", "body": b""})

    async def _refuse(self, scope: Scope, send: Send, policy: CorsPolicy,
                      origin: str | None, status: int, code: str, detail: str) -> None:
        request_id = str(uuid.uuid4())
        body = json.dumps({
            "type": f"https://anvil.dev/problems/{code.replace('_', '-')}",
            "title": code.replace("_", " ").capitalize(),
            "status": status, "detail": detail, "code": code,
            "request_id": request_id,
        }).encode()
        headers = _merge(
            [(key.encode(), value.encode()) for key, value in SECURITY_HEADERS.items()],
            # The refusal itself is never readable cross-origin when the
            # origin is not allowed; Vary still travels with it.
            policy.headers(origin)
            + [(b"content-type", b"application/problem+json"),
               (b"x-request-id", request_id.encode())],
        )
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _merge(existing: Iterable[tuple[bytes, bytes]],
           extra: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Return ``existing`` with the security headers and ``extra`` applied.

    A route may set `cache-control` or a CORS header for its own reasons; the
    security headers win, because a route cannot be allowed to weaken them by
    accident. `vary` is combined rather than replaced, since a route that
    varies on something else is still right about that.
    """
    merged: list[tuple[bytes, bytes]] = []
    overridden = {key for key, _ in extra} | {key.encode() for key in SECURITY_HEADERS}
    vary: list[str] = []
    for key, value in existing:
        lowered = key.lower()
        if lowered == b"vary":
            vary.extend(part.strip() for part in value.decode("latin-1").split(","))
            continue
        if lowered in overridden:
            continue
        merged.append((key, value))
    for key, value in SECURITY_HEADERS.items():
        merged.append((key.encode(), value.encode()))
    for key, value in extra:
        if key.lower() == b"vary":
            vary.extend(part.strip() for part in value.decode("latin-1").split(","))
            continue
        merged.append((key, value))
    if vary:
        seen: list[str] = []
        for item in vary:
            if item and item.lower() not in [s.lower() for s in seen]:
                seen.append(item)
        merged.append((b"vary", ", ".join(seen).encode()))
    return merged
