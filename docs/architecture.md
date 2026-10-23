# Anvil: product initiative and implementation architecture

## Purpose and scope

Anvil is a local-first, Apache-2.0 platform for capturing agent runs, inspecting failures, safely replaying recorded interactions, re-executing agents with controlled fixtures, injecting faults and enforcing regression suites in CI.

The core user journey is: instrument a Google ADK agent, capture and seal a failed run, inspect the timeline, export a redacted recording, replay it, re-execute with mock tools and a changed agent, compare behavior, write assertions and run them automatically in CI.

The implementation is tracked in the [product initiative](https://github.com/lewis-mcgillion/anvil/issues/1), with 14 epics and 84 implementation tasks. There is no hosted SaaS, billing, live-production agent deployment or arbitrary untrusted-code execution in the first release.

## Execution semantics

| Mode | Agent code | Model calls | Original tools | Guarantee |
|---|---|---|---|---|
| Recorded playback | Never | Never | Never | Deterministic projection of immutable stored events under a pinned schema/projection version |
| Recorded re-execution | Yes, sandboxed | No | Never | Recorded model and tool responses supplied through checked fixtures; execution itself need not be deterministic |
| Live-model re-execution | Yes, sandboxed | Only through an opt-in broker | Never | Fresh model inference with mocked tools; responses and execution may vary |

Any unmatched or ambiguous tool invocation fails closed. No execution mode silently falls back to production systems. Recorded model responses require an exact canonical request match, so changing a prompt intentionally may require a new model fixture or the opt-in live model mode. Agent code in a local container is trusted project code, not a hardened untrusted-code sandbox.

## Stack, layout and dependencies

- Go API and worker in a modular monolith. REST is specified by `api/openapi.yaml`; internal runner messages use versioned protobuf over Unix domain sockets (`proto/anvil/runner/v1/runner.proto`). Contract semantics (errors, scopes, retries, idempotency, cancellation) live in [docs/api-contract.md](api-contract.md); evolution rules live in [docs/compatibility.md](compatibility.md).
- PostgreSQL for authoritative metadata, immutable event payloads (JSONB), artifacts, suites, job leases and audit events. No Redis or dedicated object storage for MVP.
- Python capture SDK with Google ADK hooks as the first supported integration. OpenTelemetry trace/span IDs correlate capture to existing tracing. LangGraph follows after release.
- React and TypeScript with generated API bindings, accessible components and virtualized timeline rendering.
- OCI-digest-pinned Python runner container with no network, production secrets, host mounts or Docker socket. A separate model broker is the only component allowed to access explicitly configured model endpoints.
- Docker Compose for localhost installation; secure self-host examples with TLS/OIDC follow. Apache-2.0 source, signed release artifacts and SBOMs.

Suggested directories: `cmd/{anvil-api,anvil-worker,anvil-supervisor,anvil}`, `internal/{capture,events,storage,playback,replay,divergence,suites,jobs,broker,auth}`, `api/`, `proto/`, `migrations/`, `sdk/python/`, `adapters/google-adk/`, `runner/python/`, `web/`, `fixtures/`, `examples/`, `benchmarks/`, `deploy/compose/`, `docs/`.

## Storage and versioning

Core entities: `organizations`, `projects`, `principals`, `project_grants`, `api_keys`, `runs`, `run_streams`, `events`, `artifacts`, `recordings`, `agent_versions`, `reexecution_requests`, `jobs`, `reexecution_results`, `comparisons`, `divergences`, `suites`, `suite_versions`, `suite_runs`, `case_results`, `judge_results`, `audit_events` and `deletion_requests`.

All project-owned rows require a project scope. Application authorization and PostgreSQL row-level security reinforce each other. Identifiers are UUIDv7, with original OpenTelemetry trace/span IDs preserved separately. Events are immutable and carry:

```json
{
  "schema_version": "1",
  "event_id": "<uuidv7>",
  "run_id": "<uuidv7>",
  "stream_id": "<uuidv7>",
  "sequence": 12,
  "type": "tool.completed",
  "occurred_at": "2026-01-01T12:00:00.123Z",
  "trace_id": "<32-hex>",
  "span_id": "<16-hex>",
  "parent_event_ids": [],
  "invocation_id": "<uuidv7>",
  "payload": {"tool_name":"inventory.lookup","arguments":{"sku":"ABC"},"result":{"available":true},"redactions":[],"replay_safe":true}
}
```

Event types: `run.started`, `run.completed`, `run.failed`, `message.created`, `model.requested`, `model.chunk`, `model.completed`, `model.failed`, `tool.requested`, `tool.completed`, `tool.failed`, `state.checkpoint`, `capture.gap`, `fault.applied`.

Events are idempotent by event identity plus content hash; conflicting identities/sequences return HTTP 409 without partial insertion. Sequences are per stream, not a fictitious total global order. Causal parents may arrive later; sealing validates them. Capture completeness (`open`, `sealing`, `sealed_complete`, `sealed_incomplete`) differs from execution outcome (`running`, `succeeded`, `failed`, `cancelled`, `unknown`).

Defaults: max event 256 KiB, ingest batch 4 MiB or 500 events, artifact 8 MiB, run 64 MiB, SDK buffer 2000 events or 16 MiB, 30-day retention, list page 100 (max 1000). Failed capture must report gaps, never silently succeed. Artifacts and recordings carry SHA-256 hashes. No pickle or arbitrary object deserialization is accepted from recordings.

## Public API

Base `/v1`, JSON over HTTPS except loopback HTTP. Use OpenAPI as source of truth; strict request decoding, stable opaque cursors, scope-bound API keys, idempotency keys on creations, `application/problem+json` errors, `Retry-After` on throttling and `202` for asynchronous jobs.

Capture:

- `POST /projects`, `GET /projects`, `PATCH /projects/{id}`
- `POST /projects/{id}/keys`, `DELETE /projects/{id}/keys/{key}`
- `POST /projects/{id}/runs`, `POST /runs/{id}/events:batch`, `POST /runs/{id}/artifacts`, `POST /runs/{id}:seal`
- `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/events`, `GET /runs/{id}/watch` (SSE)

Playback and reproduction:

- `GET /recordings/{id}`, `GET /recordings/{id}/projection`
- `POST /recordings/{id}/exports`, `POST /projects/{id}/imports`
- `POST /projects/{id}/agent-versions`, `POST /reexecutions`, `GET /reexecutions/{id}`
- `POST /jobs/{id}:cancel`, `GET /jobs/{id}`
- `POST /comparisons`, `GET /comparisons/{id}`, `GET /comparisons/{id}/divergences`

Testing and operations:

- `POST /projects/{id}/suites`, `POST /suites/{id}/versions`, `POST /suite-runs`, `GET /suite-runs/{id}`, `GET /suite-runs/{id}/report?format=json|junit|markdown`
- `POST /projects/{id}/deletions`, `GET /deletions/{id}`
- `/health/live`, `/health/ready`, operator-scoped `/metrics`

Re-execution requests reference immutable `recording_id`, `agent_version_id`, `model_mode` (`recorded` or `live`), fixture matching policy, explicit initial state, seed, fault plan and budgets. Default limits: 120-second wall time, 20 model calls, 100 tool calls, 20k input and 10k output tokens, and USD 1.00 estimated model spend. Live mode additionally requires an approved provider profile with server-managed credentials and endpoints.

Internal runner protocol messages: `RunnerHello`, `EmitEvents`, `ModelRequest`, `ModelResponse`, `ToolRequest`, `ToolResponse`, `Heartbeat`, `RunnerFinished`. No arbitrary HTTP, filesystem, command execution or original-tool invocation primitive is exposed. The wire contract is `proto/anvil/runner/v1/runner.proto`; `internal/contracts` tests reject any field that would introduce an execution surface.

## Replay, comparison and fault design

Use RFC 8785 JSON canonicalization with explicit schema-safe handling of numbers and strings. Strict fixture matching requires logical invocation path, tool name/schema digest, canonical arguments and consumption state. Parallel identical calls must have distinct causal branch identities or produce an ambiguity error. Recorded model fixtures require canonical messages, tools, system instructions and generation settings to match. Source event timestamps must not be used as deterministic order.

Projection is a pure function of the recording and pinned version. Read-only playback never schedules execution. Re-execution produces a new candidate run with manifest including image digest, adapter, fixtures, model mode, fault plan, budgets and outcomes. Comparison reports independently:

- `deterministic`: `pass`, `fail`, `inconclusive`
- `judge`: `pass`, `fail`, `inconclusive`, `not_run`
- `infrastructure`: `ok`, `error`, `cancelled`, `timed_out`
- `alignment_confidence`: `exact`, `partial`, `ambiguous`

Diff dimensions include changed input, prompt, tool name or args, missing/extra calls, control flow, outputs, errors, budgets and capture gaps. Incomplete evidence and ambiguous alignment cannot pass. Optional LLM judges are off by default and cannot override deterministic or infrastructure failures.

Fault plans are versioned, validated and matched to logical calls. Support typed tool exceptions, timeouts, empty responses, malformed responses, JSON pointer changes, truncated streams, provider failures and prompt injection payload fixtures. Fault injection never invokes original tools. Persist every applied or unreached fault as evidence; distinguish simulated latency from measured wall time.

## YAML regression suites and CLI

The initial schema is `apiVersion: anvil.dev/v1alpha1`, `kind: RegressionSuite`. Each case pins recording file/hash, OCI agent image digest, entrypoint, adapter/version, model fixture mode, tool policy, seed, optional fault plan, budgets and assertions. Strictly reject duplicate YAML keys, unknown fields, executable expressions, network fixture URLs, unsafe aliases, out-of-directory paths and mutable image tags.

Supported assertions: exact/contains/schema/JSON-pointer output, tool called/count/arguments/order, fixture consumption, model request, execution outcome, budget, and fault application. Missing evidence is `inconclusive`, not pass. CI judge gating is optional and explicit.

CLI: `anvil doctor`, `anvil capture inspect`, `anvil recording export/import`, `anvil playback`, `anvil replay`, `anvil compare`, `anvil test validate`, `anvil test run`, `anvil jobs cancel`.

Exit codes: 0 gates passed, 1 assertion/judge gate failed, 2 invalid input, 3 infrastructure error, 4 cancelled, 5 inconclusive. JSON, JUnit and Markdown reports include artifact references and mode/version evidence. Required CI uses recorded responses or stub providers, never flaky live models as deterministic oracles.

## Security and privacy

- Redact on the SDK before buffering/transmission with allowlisted fields, credential scrubbing and configurable JSON pointers. The server applies a stricter second-pass policy. Redaction may remove replay-critical evidence; surface that limitation.
- Production tools never run during any replay. Runner gets no network, original credentials, host directories, production environment variables, Docker socket or general execution access. Fail closed if enforcement is unavailable.
- Keep model credentials solely in the broker; broker checks provider allowlists, budgets, redirect/SSRF protections and audit events. Live inference requires explicit opt-in.
- Local installation binds to loopback. Self-hosted public access requires TLS, OIDC, scoped keys, CSP/CSRF/CORS and hardened deployment templates.
- Treat traces, Markdown, imported archives, judge input and agent output as untrusted data. No agent-provided instructions can alter permissions or runner controls.
- Audit reads, exports, executions and deletions without storing raw prompts in logs. Backups, data deletion, retention and policy changes are documented and tested.
- Release gates include dependency review, SBOM, license policy, secret detection, signed artifacts and provenance.

## Testing and quality gates

Layers: unit, property testing, cross-language schema contracts, PostgreSQL integration, real ADK integration, replay isolation, API/UI browser tests, adversarial security corpus, CI fixtures, fault/drain/restart resilience, upgrade/restore, compatibility matrix and release smoke tests.

Initial targets, to be benchmarked and refined through ADR rather than represented as achieved results:

| Area | Target |
|---|---|
| Ingestion | 1000 events/second sustained for 30 minutes, 2 KiB/event |
| Ingest p95 | below 250 ms for 100-event batches |
| SDK hot path p95 | below 1 ms/event excluding network |
| Playback | same canonical projection hash across repeated runs |
| Seek p95 | below 250 ms for 10k-event recordings with checkpoints |
| Comparison | below 5 seconds for 10k-event pairs |
| Runner startup p95 | below 5 seconds for cached OCI image |
| Runner cancellation | termination within 10 seconds |
| Isolation | zero successful production-tool network access attempts |
| Secret canaries | zero present in persisted data, exported files, logs and reports |

Reference benchmark environment: Linux x86-64, 8 vCPU, 16 GiB RAM, local SSD, single PostgreSQL, documented fixture distribution. Record hardware and raw measurements. Do not claim simulated fault delays are deterministic wall-time guarantees.

## Milestones and rollout

1. **M0 Foundations:** Governance, ADRs, event/OpenAPI/runner contracts, schema fixtures, ADK hook and sandbox feasibility spikes.
2. **M1 Capture & Inspect:** Persist events with honest completeness, instrument supported ADK version, display runs and causal timelines.
3. **M2 Replay & Regression:** Pure playback; isolated runner; recorded tool/model substitution; strict matching; divergence; versioned YAML suites and offline CI.
4. **M3 Reliability & Release:** Fault injection, isolation/security hardening, ops telemetry, benchmarks, docs and reference regression suite.
5. **M4 Expansion:** Broader ADK compatibility, LangGraph adapter, optional live-model brokering/judges, hardened self-host workflows and distributed capacity only when justified.

Release acceptance: a clean checkout installs locally, captures a synthetic ADK agent failure, seals and inspects it, plays it back, safely re-executes an altered agent, detects an injected fault/behavior regression, exports a YAML test and fails or passes correctly in CI. No real tool or provider credentials are required for required recorded-mode tests.

## Issue hierarchy and definition of done

Root initiative is #1. Fourteen epic issues reference their six tasks each, and every task has explicit deliverable, acceptance checks and test obligations. Maintain published issue IDs `ANVIL-001` through `ANVIL-084` as stable references.

Every task is complete only when its feature, negative paths, cancellation behavior where relevant, contract tests, documentation and security checks pass. A baseline update creates a new immutable recording and comparison, never overwrites existing evidence. Schema evolution requires explicit compatibility and migrations. No unresolved blocker may be concealed by a mock-only demo.
