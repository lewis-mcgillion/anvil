# Investigation dashboard

The React/TypeScript shell implements ANVIL-061 (#73): API-key sessions, hash
routing, authorized project selection, explicit request errors and accessible
keyboard navigation. Project data is fetched from the API, not a built-in demo.
No new public API or event schema is introduced.

## Run locally

Use Node.js 22 and npm 9.5 or newer:

```sh
cd web
npm ci
npm run dev
```

Open the loopback address printed by Vite. Its development proxy forwards `/v1`
to `http://127.0.0.1:8080`. Use an Anvil bearer API key with `runs:read` and project
grants. `npm run build` regenerates types from `api/openapi.yaml`, type-checks and
builds `web/dist`. The `openapi-fetch` client consumes those generated paths and
response types; generated types are reproducible and remain ignored by Git.

For deployment, serve the built assets and `/v1` API through the **same origin**,
using HTTPS except on loopback. Configure the reverse proxy explicitly; there is
no user-supplied remote API URL. Serve assets with an appropriate CSP (for example
`default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self';
object-src 'none'; base-uri 'none'; frame-ancestors 'none'`) and prevent caching
authenticated API responses. The Vite server is for local development only.

## Session, scope and navigation

The key is a password input, never a URL, cookie or browser-storage value. It is
kept in memory only, cleared on sign out/reload, and sent as a bearer header only
to same-origin `/v1` requests. Fetch omits cookies and rejects redirects, including
same-origin redirects. This is an API-key session, not an OIDC implementation.
Signing in validates access through `GET /v1/projects`; a rejected key is not
treated as an authenticated success.

Only returned projects are selectable. Routes are `#/`, `#/projects/{id}` and
`#/login`; unknown routes and unavailable project IDs have explicit screens.
Deep links require sign-in and membership in the loaded authorized list.
Project switching does not authorize anything itself; server authorization and
RLS remain mandatory. Signing out unmounts project state; a new key starts with
an empty list. Pagination uses server cursors unchanged and rejects cursor
cycles. More projects can be loaded explicitly; an empty first page does not
invent a project.

Routes focus the heading, update the document title, and support browser history.
Native links, forms, selects and buttons support keyboard use; a skip link focuses
the main landmark without changing the hash route. Data and errors are rendered
as text, never raw HTML.

## Failure, cancellation and cleanup

Loading, no visible projects, unavailable project, forbidden access, expired key,
missing API, throttling and backend-not-ready states are distinct. HTTP errors
display safe local descriptions and available request IDs / `Retry-After`
metadata, not untrusted HTML response bodies. A 401 discards the session and
returns to sign-in. Other failures allow manual retry; there is no automatic
retry loop. Network and malformed-response errors remain explicit. Project
responses receive shape checks before entering state.

Each project-page fetch owns an `AbortController`. Effect cleanup aborts requests
on sign out, replacement or unmount, and late responses cannot enter a new
session. There are no persistent subscriptions, timers or background services.
React render failures have a reload boundary; reload clears the in-memory key.

## Current API blockers

The checked-in Python API currently does **not** implement `GET /v1/projects`.
It exposes a project-scoped get-run route that is not the published run-list
contract. Thus the shell builds and its API integration is browser-tested, but
the current server produces an explicit unavailable-API error, not a functioning
project list. Tests intercept API traffic to verify the published contract;
they are not proof of end-to-end Python server integration.

ANVIL-062 (#74) and ANVIL-063 (#75) are deferred: the Python API needs published
run-list filters, run detail, paginated events, authenticated SSE/resumption and
recording/projection endpoints. Once available, the explorer must independently
display capture completeness, execution outcome and eligibility; absent evidence
must never appear complete or successful. Timeline virtualization, 10k-event
scrolling and projection seek are not implemented or claimed here. The dashboard
has no execution controls and makes no execution requests.

## Browser checks

```sh
cd web
npm run build
npx playwright install --with-deps chromium
npm test
```

The Playwright runner starts its own strict-port loopback Vite server and stops
it when done. Run against an unused port 4173. Tests use synthetic keys and
intercepted contract-shaped API responses; no real credentials are required.
Coverage includes keyboard-only login/project switching/history/sign-out, skip
link, axe accessibility checks, authorization headers, reload/session isolation,
unknown routes, empty and malformed pages, HTTP and network errors, retry,
pagination/cursor cycles, stale request cancellation, redirect rejection, safe
text rendering and absence of execution calls. Chromium is the current browser
target; other browsers and real server integration are not yet covered.

CI should run the commands above after `npm ci`; the browser installation needs
the Linux libraries supplied by `playwright install --with-deps chromium`.
