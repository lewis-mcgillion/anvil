# Recorded re-execution components

The Python `anvil_server.replay` package provides a **strict fixture resolver**
and an **in-memory OCI environment approval inventory**. Neither component
runs agent code, starts containers, invokes tools, contacts model providers,
opens files, creates jobs, or connects to a database. They are integration
building blocks, not an end-to-end replay runtime. Recorded playback remains
separate: it projects events without re-executing agents.

## Coverage and remaining work

| Issue | Delivered scope | Not delivered here |
|---|---|---|
| ANVIL-039 (#47) | Canonical tool arguments, tool/schema identity, explicit causal paths, ordered single consumption, missing/repeated/reordered/ambiguous rejection | Wiring the resolver into a real isolated agent adapter |
| ANVIL-038 (#46) | Project-bound immutable approved metadata IDs, digest-pinned image references, raw OCI manifest/config verification, lock hash, adapter and exact entrypoint binding | Persistent authorized approval API, image pulls, verification of layer contents, runtime platform compatibility |
| ANVIL-040 (#48) | Exact complete model-request matching, captured completions/errors/streams, zero provider capability | Running current agent code offline through the runner protocol |
| ANVIL-037 (#45) | No runner capability added | Real OCI supervisor, narrow UDS, adversarial DNS/HTTP/metadata/host-file/privilege probes |
| ANVIL-041 (#49) | Non-recorded modes explicitly rejected by the offline session | Opt-in broker, allowlists, secret isolation, token/cost budgets, cancellation, Retry-After |
| ANVIL-042 (#50) | Versioned fixture-manifest hash and consumption evidence for a caller to persist | Durable attempts, runner leases, resource accounting, candidate trace sealing and crash/cancellation recovery |

These components do not establish that the E07 epic is complete. Docker and
Podman were unavailable in the implementation environment. Unit tests that
forbid network and process calls in the resolver are not evidence of agent
container isolation. The existing public OpenAPI, event JSON schemas and runner
protobuf are unchanged; these Python objects are not a new wire protocol.

## Strict fixture interface

```python
from anvil_server.replay import Fixture, FixtureSession

request = {
    "tool_name": "inventory.lookup",
    "schema_digest": "sha256:" + "a" * 64,
    "arguments": {"sku": "ABC"},
}
fixture = Fixture.create(
    kind="tool",
    path=("root", "inventory-branch"),
    ordinal=0,
    request=request,
    response={"status": "completed", "result": {"available": True}},
)
with FixtureSession([fixture], fixture_version="1") as session:
    response = session.resolve(
        request_id="unique-request-id",
        kind="tool",
        path=("root", "inventory-branch"),
        ordinal=0,
        request=request,
    )
    evidence = session.finish()
```

`Fixture.create` snapshots requests and responses into immutable shared
RFC 8785 canonical bytes. The session revalidates even directly constructed
dataclasses. Unknown fields in typed request/response envelopes are rejected;
arguments, completion bodies and individual chunks remain opaque JSON data.
Unsupported canonical values (non-finite numbers, unsafe integers, invalid
Unicode, cycles) fail explicitly. No normalizer, fuzzy matching, callable,
URL fetcher or production fallback is accepted.

The trusted supervisor must derive logical paths from recorded causality, not
let a runner choose another branch's identity. Calls in one path have a shared
zero-based ordinal queue across tool and model calls. Distinct paths can
interleave independently. Identical parallel requests require distinct paths;
multiple fixtures with the same path/ordinal are ambiguous and rejected. Do
not invent identities to resolve ambiguous recorded evidence. Fixtures must
come from complete, authorized, replay-safe evidence; the component does not
authenticate recordings, derive paths, or override redaction/replay eligibility.

Tool requests require exactly `tool_name`, `schema_digest` (`sha256:<64 hex>`)
and `arguments`. A response is either `{"status": "completed", "result": ...}`
or `{"status": "error", "error": {"type": "...", "message": "..."}}`.

Model requests require exactly `model`, ordered `messages` (`role`, `content`),
ordered `tools` (`name`, `schema_digest`), `system_instruction`, and `generation`.
All generation settings are compared exactly as canonical JSON; no default
settings are silently supplied. Model responses are:

- `completed`: `response`, `input_tokens`, `output_tokens`;
- `stream`: ordered `chunks` (`chunk_index`, `chunk`) and the same usage fields;
- `error`: recorded `error` with `type` and `message`.

Chunk indices must be contiguous from zero. Captured opaque chunks are returned
unchanged, never synthesized, concatenated or sent to a provider. All responses
are detached copies. A recorded error is returned as recorded data and consumes
its fixture; a matching/integrity error raises `ReplayError`.

The version-1 manifest hash includes the mode, version, sorted path/ordinal,
kind and canonical request/response hashes. Fixture input order cannot change
it. `finish()` rejects unused fixtures and otherwise returns version, strict
policy, manifest SHA-256, consumption count and this component's provider-call
count (always zero). This is not a candidate run manifest or evidence that the
agent itself made no external calls.

## Environment approval interface

`AgentEnvironment.create(image=..., manifest=..., config=..., lock=...,
entrypoint=(...), adapter=..., adapter_version=...)` requires:

- a fully qualified repository reference pinned with `@sha256:<64 hex>`,
  without a mutable tag;
- raw OCI image-manifest bytes whose SHA-256 matches that reference;
- raw Linux image-config bytes matching the manifest's config digest and size;
- pinned supported OCI layer descriptors, with no external URLs or inline data;
- nonempty lock metadata and explicit arguments matching OCI `Entrypoint + Cmd`.

OCI indexes, Docker media types, non-Linux configs, malformed/duplicate JSON
fields, absent metadata and digest mismatches fail explicitly. Registry support
is intentionally narrower than the full OCI specification. Layer bytes are
not supplied or verified here: a future launcher must verify every blob before
execution and enforce platform, mounts, environment, network and privilege
policy independently. Image metadata can contain secrets; callers must apply
capture/secret policy and must not log raw configuration.

The environment ID hashes versioned image/config/lock digests, entrypoint,
adapter and adapter version. A trusted host calls
`EnvironmentRegistry(project_id).approve(project_id, environment)` and gets
this ID. `resolve(project_id, environment_id, image=image)` requires an approved
ID and the exact image reference. Different lock/adapter metadata creates a
different ID. Immutable objects cannot be overwritten by changing a digest's
contents. This inventory is not an authorization system; project permission,
approval audit, persistence and revocation policy remain caller responsibilities.
It never resolves registry URLs or pulls images.

## Failures, cancellation and resource ownership

`ReplayError.code` is the stable machine-readable failure; its message contains
no recorded payload. Relevant codes include `FIXTURE_MISSING`,
`FIXTURE_REORDERED`, `FIXTURE_CONSUMED`, `REQUEST_REPEATED`,
`AMBIGUOUS_FIXTURE`, `FIXTURE_MISMATCH`, `FIXTURES_UNCONSUMED`,
`UNSUPPORTED_VERSION`, `LIVE_MODE_DISABLED`, `MANIFEST_MISMATCH`,
`CONFIG_MISMATCH`, `ENVIRONMENT_UNAPPROVED`, and `PROJECT_MISMATCH`.
Errors must propagate to the attempt owner; never retry a failed session or
fall back to execution. Raw recorded errors and response data are untrusted
payloads, not logging-safe messages.

Matching and consumption are serialized by a lock. Cancellation uses the same
lock, so an already resolving finite call either finishes first or observes
cancellation; no partial consumption races occur. A match failure aborts and
clears the session. `finish`, `cancel`, `close`, and context exit release stored
requests, responses and consumption indexes. Terminal sessions reject reuse.
A new attempt needs a new session. Caller exceptions also close context-owned
sessions. Registry `close()` clears its inventory; callers own the lifetime
and any persistent backing store. Neither component owns sockets, containers,
workers or provider requests to cancel.

Limits: 10,000 fixtures, 8 MiB per canonical request/response, 64 MiB retained
fixture bytes, 64 path components, bounded identity strings, 1 MiB per raw OCI
metadata/lock object. These are component bounds, not replay wall-time or token
budgets. Inputs are already decoded in-memory values; enforce transport limits
before decoding untrusted input.

## Local verification

From the repository root with the server test extras installed:

```sh
python -m pytest -q server/tests/test_replay_fixtures.py server/tests/test_replay_registry.py server/tests/test_canonical.py
python -m pytest -q server/tests
```

Tests exercise strict request/response shapes, immutable copies, canonical
numbers/Unicode, reordered and repeated calls, independent branches, concurrent
duplicate consumption, incomplete streams, registry mismatches, cancellation,
cleanup and absence of process/network/provider capabilities. PostgreSQL tests
require their separately configured integration database.
