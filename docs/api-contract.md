# Anvil API contract

This document defines the cross-cutting REST contract for the Anvil API.
`api/openapi.yaml` is the machine-readable source of truth; this document
is the human-readable policy behind it. Changes to either follow
[docs/compatibility.md](compatibility.md).

## Transport

- Base path `/v1`, JSON over HTTPS everywhere except loopback HTTP on
  local installs.
- Request decoding is strict: unknown fields, wrong types, malformed
  identifiers and out-of-range values fail with `400` and no side effects.
- Request and response sizes are bounded. Defaults: event 256 KiB, event
  batch 4 MiB or 500 events, artifact 8 MiB, run 64 MiB. Over-limit
  payloads fail with `413` and no partial writes.

## Errors

All errors use RFC 9457 `application/problem+json`:

```json
{
  "type": "https://anvil.dev/problems/forbidden",
  "title": "Forbidden",
  "status": 403,
  "detail": "Scope replay:write is required for this operation.",
  "code": "forbidden",
  "request_id": "018f3c4a-9c6b-7c2e-9f5a-2d1f0b9a8c7d"
}
```

`code` is the stable machine-readable error code and is safe to branch
on. `detail` is human-readable, redacted, and may change. `request_id`
correlates with server logs and audit events.

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | Unknown field, bad type, invalid enum, malformed cursor, unsafe suite YAML. |
| 401 | `unauthenticated` | Missing or invalid API key. `WWW-Authenticate` header set. |
| 403 | `forbidden` | Authenticated but missing the required scope. |
| 404 | `not_found` | Resource absent or outside the caller's project scope. |
| 409 | `conflict` | Duplicate idempotency key with a different payload; conflicting event identity/sequence (no partial insertion); invalid state transition (sealed run, finished job). |
| 413 | `payload_too_large` | A documented size bound was exceeded. |
| 429 | `throttled` | Quota or rate limit; honor `Retry-After`. |
| 500 | `internal` | Unexpected server fault; safe to retry idempotent requests. |

Failures are explicit: any unmatched or ambiguous replay fixture, missing
evidence or malformed input is an error, never a silent pass or a fallback
to production systems.

## Authentication and scopes

API keys are bearer tokens bound to a project and a scope set. Only the
key hash is stored; the secret is returned once at creation.

| Scope | Permits |
|---|---|
| `capture:write` | Create runs, ingest event batches, upload artifacts, seal runs. Cannot read or replay. |
| `runs:read` | Read projects, runs, events, recordings, projections, jobs, comparisons; export recordings. |
| `replay:write` | Register agent versions, request re-executions, imports, comparisons, job cancellation. |
| `suites:write` | Create suites, compile versions, start suite runs. |
| `suites:read` | Read suite runs and reports. |
| `admin` | Manage projects, API keys and deletions. |
| `metrics:read` | Operator `/metrics`. |

Capture keys are intentionally write-only so a compromised agent host
cannot exfiltrate recordings or trigger execution.

## Retries and throttling

- Retry only idempotent operations (any `GET`, or any request carrying an
  `Idempotency-Key`). The server deduplicates by key.
- On `429`, wait at least the `Retry-After` seconds before retrying. Use
  exponential backoff with jitter for repeated throttling.
- On `5xx` or a network drop, retry with the same idempotency key; a
  replayed key returns the original result without repeating side effects.
- `409` means a real conflict (payload mismatch, conflicting event, state
  transition). Do not retry unchanged.

## Idempotency

Creating resources requires an `Idempotency-Key` header (8–128 chars,
`[A-Za-z0-9_-]`; UUIDv7 recommended). The first request's response is
stored; retries with the same key and identical payload return that
original response; reusing the key with a different payload returns `409`.

Event ingestion is additionally idempotent by event identity plus content
hash: identical events in a retried batch are acknowledged as duplicates
without re-insertion, and any conflicting event identity or sequence
fails the whole batch with `409` — batches are atomic, never partial.

## Asynchronous jobs, cancellation and cleanup

Long-running work (sealing, export, import, re-execution, comparison,
suite runs, deletion) returns `202` with a job handle
(`job_id`, `job_url`, `status`). Poll `GET /jobs/{id}` until a terminal
state (`succeeded`, `failed`, `cancelled`).

Cancel with `POST /jobs/{id}:cancel`:

- Cancellation is asynchronous and idempotent: the job moves to
  `cancelling`, then `cancelled`; repeating cancel returns `200`.
- Cancelling a finished job is a `409` conflict.
- The supervisor terminates the runner (target: 10 seconds) and always
  reclaims resources — container, Unix socket, fixture mounts, job lease —
  on cancellation, failure or timeout. Cleanup is the supervisor's duty,
  never the caller's.

The runner protocol applies the same rules: `Cancel` stops the runner,
which must reply `RunnerFinished(CANCELLED)`; the supervisor then closes
the socket and releases the sandbox.

## Safety invariants (REST)

- Playback endpoints (`GET /recordings/...`, projection) are read-only and
  never schedule execution or broker calls.
- Re-execution only references immutable, pre-registered artifacts:
  digest-pinned OCI images (mutable tags are rejected), recorded
  recordings, checked fixtures and validated fault plans.
- No request body or protocol message carries arbitrary commands, URLs,
  host paths, environment variables or credentials. Original tools never
  run in any replay mode.
