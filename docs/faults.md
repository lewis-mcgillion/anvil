# Offline fault injection

`anvil_server.faults` implements a bounded, in-memory fault engine over **supplied
mock fixtures only**. It has no production dispatch callback, provider, network,
filesystem lookup, expression evaluation or original-tool execution capability.
It is not yet connected to the replay runner, broker, REST API or database.

## Version and compatibility

The envelope is the existing immutable manifest v1 shape:
`{"fault_plan_version":"1","faults":[...]}`. Every fault has `fault_id`,
`fault_type`, `target_invocation_path` and typed `parameters`. The narrower
offline input profile is published in
[`fixtures/faults/v1/plan.schema.json`](../fixtures/faults/v1/plan.schema.json);
it is generated from `FaultPlan.model_json_schema()` and checked for drift.
No frozen event, manifest, OpenAPI or runner contract is changed.

Unknown versions, fields, action types, coercions, duplicate JSON keys, duplicate
fault IDs and multiple faults targeting the same path are errors. Input cannot
set the manifest's output-only `applied` flag. There is **no implicit precedence
or composition**: exact selectors have one unambiguous action, and plan list
order controls evidence order only. Wildcards, filesystem paths, URLs,
traversal segments and executable expressions are not selectors. Parallel
branches and retries must have distinct logical paths, such as
`root/branch-a/lookup/attempt-1`.

The seed is a required engine argument, kept separately in the existing
re-execution manifest `seed` field. It is an integer from 0 through
9007199254740991. Preserve it together with the normalized plan and its hash.

The offline JSON subset is null, boolean, string, array, object and integers
within the interoperable IEEE-754 safe range. Floating-point values (including
NaN and infinities), non-string keys, surrogate code points, tuples, arbitrary
objects and unsafe integers fail explicitly. This restriction avoids claiming
general RFC 8785 numeric support without a shared JCS implementation. Within
this subset, canonical bytes follow JCS, including UTF-16 object-key ordering
and UTF-8 output. It does not change the general event JSON contract; callers
must reject unsupported fixtures rather than silently round or stringify.

## Supported actions

| `fault_type` | Parameters beyond latency | Result |
|---|---|---|
| `tool.exception` | `error_type`, `message`, optional `retryable` | Typed failure; no exception class is dynamically imported |
| `tool.timeout` | None | Typed retryable `timeout`; never sleeps |
| `tool.empty_response` | `response`: `null` (default), `object`, `array`, `text` | Corresponding empty JSON value |
| `tool.malformed_response` | `text` | JSON string containing deliberately malformed tool content |
| `json_pointer.patch` | RFC 6901 `pointer`, JSON `value` | Replace existing member/array element or whole response at empty pointer |
| `stream.truncate` | `max_bytes` | UTF-8 text prefix without a split code point |
| `prompt.injection` | `text` | Replace text fixture with inert adversarial text |

Every action accepts `simulated_latency_ms` and `jitter_ms` (default zero).
Their sum must not exceed 120000 ms. Truncation and prompt injection require a
recorded text response. Pointers support `~0` and `~1` escapes and canonical
array indices; missing members, insertion, array append, scalar traversal,
`..`, `.` and prototype-pollution segments are rejected. No path is ever read
from disk. Source objects are snapshotted and never mutated. Preflight checks
all mutations with available fixtures before consuming any call.

`model.provider_failure` is deliberately unsupported and fails validation.
Brokered rate-limit/disconnect/stream faults require actual provider dispatch
and usage accounting; the offline engine does not simulate that integration.

## Calling interface

```python
from anvil_server.faults import FaultEngine, Invocation, MockFixture, parse_plan

plan = parse_plan(b'{"fault_plan_version":"1","faults":[]}')
request = Invocation(
    target_invocation_path="root/lookup/attempt-1",
    tool_name="inventory.lookup",
    schema_digest="a" * 64,
    arguments={"sku": "ABC"},
)
fixture = MockFixture(request=request, response={"available": True})
engine = FaultEngine(plan, [fixture], seed=42)
result = engine.invoke(request)
report = engine.finish()
```

`invoke()` first checks exact logical path, tool name, schema digest, canonical
arguments and unconsumed fixture identity, **even for faults that return an
error**. Unknown, changed, repeated and ambiguous calls fail closed. There is
no fallback to original tools. A typed injected failure consumes its fixture.
Retry/fallback policy belongs to the calling agent; a retry uses a separately
supplied fixture with a distinct logical path. `ToolResult.error` carries
`type`, `message` and `retryable`; `response` is `None` for failures.

`execute(plan, fixtures, ordered_calls, seed=...)` is a convenience batch helper.
The caller supplies stable **causal logical order**, not source timestamps.
This version schedules sequential calls only; it does not invent an ordering
for causally ambiguous parallel calls. Latency jitter is the first 64 bits of
SHA-256 of canonical `[seed, fault_id, target_invocation_path]`, modulo
`jitter_ms + 1`. Simulated time is the cumulative latency in supplied call
order. There is no sleep, wall-time claim, random global state or dependency
on Python's randomized hash. Repeating the normalized plan, seed, fixtures
and supported call order reproduces decisions and evidence hashes.

## Evidence, failures and cancellation

`report()` returns an immutable snapshot: normalized plan hash, seed, engine
status, consumed call count, unused fixture paths, simulated clock, and one
`Evidence` per configured fault. Each entry is explicitly `applied` or
`unreached`, with latency and simulated completion time. `all_faults_applied`
is false for unreached faults and non-completed engines; missing evidence is
never a passing assertion. Reports contain no fixture payloads or fault text.
Plan hashes do not hash fixture contents and must not be used as recording
identities; the caller must pin immutable fixture/recording hashes separately.

Plans are limited to 1 MiB and 100 faults; execution accepts at most 100 mock
fixtures and 100 calls. Each argument/response/replacement is at most 256 KiB
of canonical JSON, with 10000 nodes and depth 32. The combined mutated output
is rechecked, not only the replacement. Strict plan parsing bounds bytes
before decoding. Plan and fixture values supplied as models are revalidated
and snapshotted at engine construction.

Invalid plans raise Pydantic `ValidationError`/`ValueError` before execution.
Invalid fixture matching or mutation raises `FaultExecutionError` and makes
an active engine terminal with status `error`. Constructor/preflight failures
create no engine or consumed state. Typed injected tool failures are results,
not infrastructure exceptions. `finish()` seals the engine; it cannot be
reused. Diagnostic `report()` remains available after a terminal error.

An optional `cancelled: Callable[[], bool]` is checked before work and before
committing fixture consumption/evidence, and at `finish()`. Cancellation raises
`FaultCancelled`, makes the engine terminal and preserves already committed
evidence. The in-flight fixture remains unconsumed. No sleep, task, connection,
file or process needs cleanup. The engine is single-consumer, not thread-safe;
bounded synchronous work cannot be interrupted halfway by an async event loop.
Callers must discard failed batch output; use the incremental engine when
partial evidence is required.

Persistence is **not implemented** here. Integrators must record the report
and mapped `fault.applied` events in a new candidate run, supplying real
UUIDv7 invocation/event/run/stream identities, sequence, timestamps,
redactions and replay-safety flags according to the frozen event schema.
Unreached selectors require report/result evidence, not fabricated applied
events. Broker accounting and durable assertions remain separate work.

## Checks

```sh
.venv/bin/python -m pytest server/tests/test_fault_contracts.py server/tests/test_fault_engine.py
```

The tests cover retry/fallback with fixtures, strict matching, inert malicious
text, duplicate/conflicting selectors, byte/depth/node limits, invalid pointer
corpus, UTF-8 truncation, immutable originals, cancellation before commit,
terminal lifecycle, unreached evidence and repeated seeded evidence hashes.
