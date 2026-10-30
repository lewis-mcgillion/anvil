# Python capture SDK

The capture-only SDK ships alongside the frozen validators in the
`anvil-schema` distribution. Install with `pip install ./sdk/python`, or
`pip install -e './sdk/python[test]'` for development. Python 3.11 or newer
is required. `anvil` and `anvil_schema` ship PEP 561 type markers. Importing
the package requires no credentials and starts no threads; constructing a
client starts one exporter worker.

## Capture

```python
from anvil import AnvilClient, RedactionPolicy

with AnvilClient(
    "http://127.0.0.1:8080/v1",
    api_key="project-capture-write-key",
    project_id="018f3c4a-9c6b-7c2e-9f5a-2d1f0b9a8c7d",
    redaction=RedactionPolicy(
        allow=("/content", "/arguments", "/result"),
        deny=("/arguments/customer/password",),
        patterns=(r"customer-secret-[A-Za-z0-9]+",),
    ),
) as client:
    with client.run("inventory-agent") as run:
        parent = run.message("user", "Check inventory")
        with run.span(parent_event_ids=(parent,)):
            run.tool_requested("inventory.lookup", {"sku": "ABC"})
            # Application code owns the actual invocation, not the SDK.
            run.tool_completed("inventory.lookup", {"sku": "ABC"}, {"available": True})
    client.flush(timeout=5)
```

`AsyncAnvilClient` has asynchronous client context management, `flush` and
`close`; use `async with client.run(...)` for nonblocking run creation.
Hooks themselves are synchronous local operations: they sanitize, validate
and enqueue without network waits. `run_id=...` attaches a fresh capture
stream to an existing run instead of creating one.

Run contexts emit `run.started` and a successful or failed terminal event,
but **do not seal a recording or claim complete capture**. Sealing remains
an explicit server operation. `current_run()` uses task-local ContextVars.
Nested contexts restore the parent on exceptions and cancellation. Child
asyncio tasks inherit the context: finish them before exiting the run.
Contexts cannot be reused or shared as context managers across tasks.

Use one `run.span()` for each logical model/tool invocation, with its request,
chunks and terminal hook inside it. Each span supplies fresh invocation and
span IDs, optionally naming causal parent event IDs. Nested spans restore
their outer invocation. Set `trace_id` to correlate with application-owned
tracing. There is no OTEL semantic exporter/importer here.

Hooks include `message`, `model_requested`, `model_chunk`, `model_completed`,
`model_failed`, `tool_requested`, `tool_completed`, `tool_failed`, and
`checkpoint`. `emit(type, payload)` supports other frozen event shapes,
subject to the same policy and validation. UUIDv7 IDs and UTC timestamps
are generated locally. Delivery sequences are separate per stream, not a
global order. This SDK never invokes tools, models, subprocesses or replay.

## Privacy before buffering

Context inputs and hook payloads are sanitized **before SDK retention,
validation, serialization, queueing or HTTP transmission**. The exporter
sees only detached sanitized copies. Later mutation of application input
cannot change queued evidence.

Top-level fields are allowlisted by frozen event type. Arbitrary content
(`input`, `output`, `content`, `request`, `response`, tool arguments/results,
state, fault parameters, labels and details) is omitted by default. Opt in
only the payload-relative RFC 6901 pointers you intend to capture. An allow
pointer includes descendants; deny pointers take precedence, support escaped
object keys and array indices, and have no wildcard semantics. Nested object
allowlists omit unselected siblings. Unknown top-level fields are discarded.

Credential-shaped keys (authorization, cookies, passwords, secrets, tokens,
API keys and credentials) are recursively scrubbed even in allowed data.
Built-in patterns scrub bearer credentials, common inline credential
assignments and `sk-` keys. Custom regular expressions also scrub metadata,
values and matching object keys. This is not a universal secret detector:
configure domain-specific patterns before opting content in. Invalid JSON,
nonfinite captured numbers and invalid schema shapes fail explicitly.
Omitted values need not be JSON-serializable.

Exception text is never stringified or captured. Failed hooks record a
generic code and redacted message. **Streaming chunks are always omitted**,
even if `/chunk` is allowed: credentials can straddle chunk boundaries and
cannot safely be scrubbed fragment by fragment without raw buffering.
Capture a sanitized full response separately if needed. Redaction pointers
force `payload.replay_safe=false`; omissions are never claimed replayable.
Error and chunk evidence is intentionally replay-ineligible.

## Bounds, retries, errors and cleanup

`ExporterConfig` defaults to 2,000 events / 16 MiB including in-flight data,
with at most 500 events / 4 MiB per same-run, same-stream batch. Events over
256 KiB are dropped with a `size_limit` signal. The event path never waits
for queue space. Overflow emits a static `CaptureWarning`, increments sticky
loss counters and reserves out-of-band `capture.gap` evidence. At most
128 pending stream summaries (or `max_events`, whichever is smaller) are
retained separately from the data budget. Further losses remain explicit
in local status even if that bounded control budget is exhausted.

The worker sends immutable JSON with an `Idempotency-Key`. Timeouts,
transport errors, 429 and 5xx retry identical bytes and keys, at most three
attempts by default. HTTP attempts use a one-second socket timeout and a
64 KiB response bound. Retry-After seconds and HTTP dates are honored,
with exponential backoff and jitter; if the wait exceeds the five-second
default retry-delay budget, capture fails instead of retrying early or
stalling the application. 4xx conflicts, redirects and malformed
acknowledgements are explicit failures, not unchanged retries. Positive
acknowledgements must account for every event as accepted or duplicate.

Gap summaries keep delivered sequences contiguous under queue pressure.
Once a network attempt has an ambiguous result, the server may have accepted
events whose acknowledgement was lost; subsequent gap delivery can conflict
or fail too. **Remote gap persistence cannot be promised when ingest is
unavailable.** Sticky `status.incomplete`, loss/failure counts and
`CaptureIncomplete` from `flush`/`close` are authoritative locally; never
seal such a run as complete. Raw requests and server error bodies are
excluded from SDK errors and warnings.

`flush(timeout)` waits only to its deadline, raises if draining fails, and
raises after historical loss even if gap evidence was delivered.
`close(timeout)` rejects new data, attempts a bounded drain, then interrupts
backoff and stops the worker. Requests in progress finish on their transport
timeouts. Custom transports **must** honor the timeout and raise
`CaptureError` on network failures; Python cannot kill arbitrary blocking
custom code. The built-in transport closes each connection on success/error,
requires HTTPS except loopback HTTP, and never follows redirects.

Misuse/schema failures raise `CaptureError` or `ValueError`. If an agent is
already raising, terminal capture/cleanup failures warn instead of replacing
its exception. `CancelledError` propagates unchanged after terminal failure
capture and context restoration. Cancelling async flush/close does not
cancel its bounded cleanup thread or the exporter; close the client later
if its lifetime continues. Cancelling in-flight run creation cannot undo
server creation: no context activates, but an open server run may remain.

## Compatibility, packaging and verification

The distribution name/version (`anvil-schema` 1.0.0) and SDK
`anvil.__version__` (0.1.0) are preserved; the previously unshipped `anvil`
namespace is now included. The minimum interpreter moves from the original
validator-only 3.10 declaration to the repository's Python 3.12 baseline.
REST `/v1` and event `schema_version="1"` are unchanged. Other constructor
schema versions fail explicitly rather than downgrading. Unknown additive
response fields are ignored; unknown event types/versions fail closed.

`uv build sdk/python --out-dir sdk/python/dist` builds an sdist and a wheel
from it. Build hooks copy all eight authoritative `schemas/v1` documents
into package resources, so installed validation needs no source checkout.
Source installations use authoritative repository schemas.
`requirements.lock` records the direct pinned validator dependency, not a
transitive resolver lock.

```sh
uv venv sdk/python/.venv --python 3.12
uv pip install --python sdk/python/.venv/bin/python -e './sdk/python[test]'
sdk/python/.venv/bin/python -m pytest sdk/python/tests -q
```

Tests cover nested contexts, mutable inputs, exceptions, async cancellation,
rejected schemas, canaries in streams/errors, queue/byte pressure, oversized
events, retries, Retry-After, idempotency, invalid acknowledgements, explicit
gaps, cleanup deadlines and loopback HTTP without production credentials.
HTTP tests exercise the published capture contract, not a claim that the
current Python server already provides all routes or registers SDK streams.
Server integration must supply run creation, stream registration and atomic
ingestion matching those contracts before deployment.

An opt-in durable crash spool, OTEL semantic mapping/import, release
publication and a CI Python-version matrix are **not implemented** here.
There is no disk queue or crash-recovery guarantee.
