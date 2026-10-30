# Google ADK adapter

The adapter package is `adapters/google-adk` (`anvil-google-adk`, adapter
version `0.1.0`, Apache-2.0). It uses the **real `google-adk==2.13.0`**
package, not a local imitation of ADK. The supported/tested runtime is Python
3.12 on Linux; the tested dependency matrix, including `google-genai==2.34.0`,
is pinned in `adapters/google-adk/requirements.lock`. Other ADK versions are
rejected by constructors with `Ineligible`, rather than assumed compatible.

## Installation and offline conformance

From the repository root, with `uv` available:

```sh
uv venv --python 3.12 .venv-adk
uv pip sync --python .venv-adk/bin/python adapters/google-adk/requirements.lock
uv pip install --python .venv-adk/bin/python --no-deps -e adapters/google-adk
.venv-adk/bin/python -m pytest -q adapters/google-adk/tests
```

Tests use actual ADK `Runner`, `App`, `LlmAgent`, `FunctionTool`,
`SequentialAgent`, `ParallelAgent` and `InMemorySessionService`. A local
`BaseLlm` implementation supplies deterministic model responses through the
real framework. No provider credentials or model network calls are needed.
ADK deprecates SequentialAgent/ParallelAgent in this release; the tests
deliberately exercise their still-supported nested/parallel behavior.

The interception spike installs a real function tool whose body writes a
side-effect marker and raises a fatal exception. Real `before_model_callback`
and `before_tool_callback` hooks substitute responses, and the marker must
remain absent. This proves these hook boundaries for this pinned version,
**not** sandbox isolation for arbitrary Python agent code.

## Passive capture

```python
from anvil_adk.plugin import CapturePlugin
from google.adk.apps import App
from google.adk.runners import Runner

# sink(kind, payload, correlation) must redact BEFORE buffering or transmission,
# construct an Anvil v1 envelope, and surface capture/transport failures.
plugin = CapturePlugin(sink)
runner = Runner(
    app=App(name="sample", root_agent=agent, plugins=[plugin]),
    session_service=session_service,
)
# Consume runner.run_async(...) and always await runner.close() in finally.
```

`CapturePlugin` returns no replacement values. The conformance suite compares
instrumented and uninstrumented final responses and original-tool call counts.
It emits v1 `message.created`, `model.requested`, `model.chunk`,
`model.completed`, `model.failed`, `tool.requested`, `tool.completed` and
`tool.failed` payloads. Complete ADK requests include messages, generation
configuration, system instructions and tool declarations; executable
`tools_dict` objects are excluded. Framework response/message metadata is
preserved inside existing free-form payload fields. No public event schema is
changed.

Correlation is passed **separately** from the closed event envelope/payload:
`adk_version`, `adk_invocation_id`, `branch`, `agent_name`,
`function_call_id` (and `adk_event_id` for framework messages). ADK invocation
IDs are not Anvil UUIDv7 IDs. Message correlation preserves the emitting
event's branch/author rather than the root agent's context. The sink
must map them to stable causal invocation/stream IDs and preserve branch
identity; do not identify parallel calls using name/arguments alone.
Streaming chunks have contiguous per-model-call indices starting at zero.
This is not a fabricated global event order.

All emitted payloads conservatively set `replay_safe: false`: hook capture
alone does not prove complete eligibility. A sink must not silently promote
them. The plugin does not own run creation, sealing, run-level outcomes,
buffering, redaction, SDK lifecycle or network resources. Those must be supplied
by the SDK/runner integration. Until that bridge is wired, this is a tested
payload integration, not end-to-end persisted capture.

### Errors, cancellation and cleanup

Model/tool exceptions emit structured errors and then propagate through ADK
without recovery. Tool cancellation records `CancelledError`; cancellation
is never converted into success. Sink/serialization errors propagate (ADK
may wrap callback exceptions); they must make capture incomplete, never be
swallowed by the application. Do not retain raw prompts in error logs.
Bookkeeping is cleared after successful/failed invocations and on `close()`.
Always close the runner in `finally`, including cancellation and early stream
termination. The plugin owns no files, sessions, sockets or background tasks.

## Replay substitution boundaries

`anvil_adk.replay.ReplayModel` and `ReplayTool` are actual ADK `BaseLlm` and
`BaseTool` implementations. Construct the replay graph with these objects
instead of production providers/tools; neither holds a reference to an
original implementation, and neither has an original-call fallback.
Use tool declarations from the recording, not live tool discovery.

Both require an asynchronous `Resolver`:

- `model(request) -> response`: the complete JSON request and a JSON
  `LlmResponse` object containing content or an explicit error.
- `tool(name, declaration, arguments, correlation) -> result`: declaration,
  arguments and ADK causal context, returning a JSON object (including `{}`).

Missing/ambiguous fixtures must **raise**, never return `None`. Invalid
responses, partial model responses in non-streaming replay, and streaming
model replay fail explicitly. Resolver cancellation propagates.

The resolver is an integration boundary, **not an implemented fixture store**.
The runner must provide RFC 8785 exact request matching, tool-schema digests,
consumption state, stable logical paths (including repeated invocations),
ambiguity rejection, budgets and typed IPC. Passing an arbitrary resolver is
trusted application code and cannot confer the runner's no-network guarantee.
No live-model replay or provider fallback is implemented here.

## Explicit JSON-only session state

`export_state(session.state)` returns an adapter-local snapshot:

```json
{"schema_version":"1","adapter":"google-adk","state":{"counter":4}}
```

`import_state(snapshot)` returns a detached JSON dictionary suitable for
`await session_service.create_session(..., state=import_state(snapshot))`.
`fixtures/adk/state-v1.json` is exercised against a real ADK session and
state-mutating tool. Snapshot functions reject missing state, unknown fields,
unsupported versions/adapters, opaque objects, bytes/pickle, tuples, non-string
keys, nonfinite floats and cycles. They never deserialize Python objects.
`Ineligible` supplies the rejection reason. No implicit empty-state fallback
is permitted.

These are **state** snapshots, not serialization of an entire ADK session:
event history, artifacts, authentication, process globals and memory services
are not reconstructed. Callers must export required initial state explicitly
before execution, apply SDK redaction, bound its size and persist it as
`state.checkpoint` evidence. Redaction of required state makes replay
ineligible. No external resources need cleanup for these pure copy operations.
The adapter-local snapshot is not a new public recording schema.

## Supported matrix and uncovered gaps

| Surface | Evidence / limitation |
|---|---|
| Plain LlmAgent model/tool interception | Real Runner success and fatal side-effect canary tests |
| Passive model/tool/message capture | Outcome parity and v1 payload schema validation |
| Model/tool errors; async tool cancellation | Real Runner propagation and cleanup tests |
| Nested SequentialAgent/ParallelAgent | Distinct branch/agent metadata for identical calls |
| Model streaming capture | Real SSE-configured Runner, ordered chunks and completion |
| Safe state reconstruction | Real session initialization, tool mutation and snapshot isolation |
| Typed non-streaming substitutions | Real Runner, resolver failures and malformed responses rejected |
| Unsupported ADK version | Explicit constructor rejection tests |
| SDK envelope, redaction and persisted capture | Not yet wired; caller-owned sink required |
| Full runner replay / sandbox / strict fixture matcher | Not wired by this adapter; boundaries alone are not eligibility |
| Streaming replay, bidirectional/live audio/video | Unsupported; streaming replay explicitly rejected |
| Toolsets, MCP/OpenAPI tools, agent-as-tool | Discovery/setup may execute before hooks; not proven safe |
| Custom agents/workflows, callable instructions, callbacks | Arbitrary Python can execute outside boundaries; not replay-safe |
| Agent callbacks or later plugins modifying values | Capture hooks may observe pre-mutation values; not proven complete |
| Provider-internal retries and recovery callbacks | No attempt-level completeness guarantee |
| Repeated identical calls within the same branch | Sink/resolver must allocate logical occurrences or reject ambiguity |
| Session event history, artifacts, auth and remote memory | Not included in state snapshots |
| Early stream close / model cancellation capture | Close releases bookkeeping; complete terminal evidence not guaranteed |

Install capture last among passive plugins; do not claim exact capture when
agent callbacks or other plugins mutate model/tool values. The supported
boundary does not intercept arbitrary filesystem/network side effects inside
instructions, callbacks, custom tools' setup, model construction or agent
code. Full replay must construct a checked substitution-only graph and run in
the separately enforced isolated runner.

Issue coverage: ANVIL-025 (#31) has a real pinned feasibility spike and gap
inventory; ANVIL-029 (#35) has explicit JSON-only state hooks. ANVIL-026/027
(#32/#33) have tested capture/branch/streaming primitives but await SDK
integration and broader completeness. ANVIL-028 (#34) has typed substitution
boundaries but awaits checked runner/fixture integration. ANVIL-030 (#36) has
a runnable real-framework conformance suite, not a complete retries/version
matrix or CI wiring. No issue is closed by this document.
