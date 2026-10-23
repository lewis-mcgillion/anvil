# Anvil threat model

This document is the security counterpart to [architecture.md](architecture.md)
and the [architecture decision records](adr/README.md). It identifies what
Anvil protects, who it protects against, the trust boundaries involved, and
the threat scenarios the design must handle — with the controls that
mitigate each, the residual risk that remains, and the operational
expectations for errors, cancellation and cleanup.

Status: Accepted, maintained. A change to authentication, authorization,
isolation, redaction or tool execution must update this document in the
same change, per the security rule in [architecture.md](architecture.md).

## 1. Scope and security objectives

Anvil captures agent runs, stores them as immutable evidence, replays them
deterministically, re-executes agents in an isolated runner, optionally
brokers live model calls, and gates regressions in CI. The security
objectives are:

1. **O1 — No production effect from replay.** Replay (playback or
   re-execution) never executes production tools, makes production network
   calls or mutates production state (ADR-0001, ADR-0004, ADR-0005).
2. **O2 — Secret containment.** Captured secrets are redacted before
   persistence; provider credentials exist only in the broker; no secret
   appears in storage, exports, logs, reports or error messages.
3. **O3 — Tenant isolation.** One project's data, runs, keys and budgets
   are unreachable from another project, enforced at two independent
   layers (ADR-0002, ADR-0003).
4. **O4 — Evidence integrity.** Stored events and recordings are immutable
   and content-hashed; malicious or corrupted inputs are rejected rather
   than trusted (ADR-0003, ADR-0005).
5. **O5 — Honest degradation.** Overload, missing fixtures, incomplete
   capture and cancelled work are explicit failures — never silent
   successes.

Out of scope for the first release (stated openly, not implied): a hosted
multi-tenant SaaS, live-production agent deployment, and arbitrary
untrusted-code execution. Agent code in the local runner is trusted
project code; the runner is hardened against escapes and egress but is not
a certified hostile-code sandbox (see T1 residual risk).

## 2. System overview and trust boundaries

Trust domains and boundaries (defined in ADR-0002):

| Boundary | Untrusted side | Trusted side | Crossing mechanism |
|---|---|---|---|
| B1 Capture | Agent process, network clients | Anvil API | Scoped API keys, strict decoding, server-side redaction |
| B2 Data | Stored events, recordings, archives, Markdown, judge input, agent output | Projection, rendering, comparison | Versioned schemas, safe parsing, output escaping |
| B3 Execution | Agent code in runner | Supervisor, host | OCI isolation, no network/secrets/mounts, protobuf over UDS |
| B4 Model egress | Runner, API, workers | Broker, providers | Allowlisted endpoints, budgets, SSRF protection |
| B5 Tenancy | Other projects/organizations | Project data | Application authorization + PostgreSQL RLS |

Primary assets: recorded events and artifacts (may contain personal data),
recordings/exports, API keys, provider credentials, CI gate outcomes, and
the integrity of projection/comparison results.

## 3. Adversary model

- **A careless or compromised agent developer** whose instrumented app
  sends secrets, oversized payloads or garbage to the capture API.
- **A malicious recording author** who crafts a recording, export bundle,
  YAML suite or fault plan to attack whoever opens, imports or replays it.
- **A curious or malicious tenant** in a shared self-hosted deployment who
  attempts to read or interfere with another project's data.
- **A prompt-injection attacker** whose content lands inside a recorded
  tool result and is later replayed or judged.
- **An insider or script with a leaked key** attempting to spend model
  budget or exfiltrate via live mode.
- **A supply-chain attacker** tampering with dependencies, images or
  release artifacts.

Insiders with legitimate project owner access, host root on the machine
running the platform, and denial of service against a user's own local
install are out of scope (their own resources); the controls still limit
blast radius.

## 4. Threat scenarios and mitigations

Each scenario lists: the boundary, the attack, mitigations (with the ADR
that owns them), residual risk, and detection/audit evidence.

### T1 — Untrusted or compromised agent code (B1, B3)

**Attack.** Agent code running under re-execution attempts to reach the
network (DNS, HTTP, cloud metadata endpoints), read host files or
environment variables, exfiltrate fixture contents, consume unbounded
CPU/memory/processes, or abuse the supervisor channel to execute commands.
Instrumented application code may also stream secrets or unbounded garbage
into the capture path.

**Mitigations.**

- Runner container has no network, no production environment variables, no
  host mounts and no Docker socket; enforcement is verified at startup and
  the job fails closed if unavailable (ADR-0004).
- The runner protocol exposes only `RunnerHello`, `EmitEvents`,
  `ModelRequest`, `ModelResponse`, `ToolRequest`, `ToolResponse`,
  `Heartbeat`, `RunnerFinished` — no arbitrary HTTP, filesystem, command
  execution or original-tool primitive (ADR-0004). Tool requests are
  answered only from strict fixtures (ADR-0005).
- Resource limits (CPU, memory, process count) and budgets (wall time,
  model/tool call counts, tokens, spend) are enforced with termination on
  breach (ADR-0004, ADR-0006).
- Capture ingestion is bounded (event 256 KiB, batch 4 MiB/500 events,
  artifact 8 MiB, run 64 MiB) with `Retry-After` overload semantics, so a
  runaway app cannot exhaust the server; overflow yields `capture.gap`,
  never a false "complete" (ADR-0003).
- SDK redacts before buffering/transmission; the server applies a stricter
  second pass (ADR-0002).

**Residual risk.** The runner is trusted-project-code isolation, not a
certified hostile-code sandbox: a container-escape-class vulnerability in
the container runtime is outside Anvil's control. Do not run deliberately
malicious code and expect containment; stronger sandboxes (gVisor /
microVM) are an explicitly deferred decision (ADR-0004).

**Detection.** Runner manifest (image digest, adapter, budgets) sealed per
run; adversarial egress/privilege probes are part of the isolation test
corpus; infrastructure failures are recorded and never read as passes.

### T2 — Malicious recordings, exports and suites (B2)

**Attack.** A crafted recording, `.anvil` archive, YAML suite or fault plan
is opened, imported or replayed to attack the reader: archive path
traversal, decompression/size bombs, schema confusion, duplicate YAML keys,
unsafe aliases/tags, executable expressions, mutable image tags, network
fixture URLs, oversized payloads, hash tampering, or stored XSS/Markdown
injection rendered in the dashboard.

**Mitigations.**

- All persisted or imported content is untrusted data (boundary B2):
  validated against versioned schemas, decoded strictly, and escaped on
  render. No agent-provided instructions can alter permissions or runner
  controls (ADR-0002).
- Archives are parsed safely: manifest with schema versions, SHA-256 hash
  verification, rejection of traversal, bombs, oversized payloads and
  tampering; no pickle or arbitrary object deserialization is accepted
  (ADR-0003).
- YAML suites are parsed strictly: duplicate keys, unknown fields,
  executable expressions, unsafe aliases, out-of-directory paths, network
  fixture URLs and mutable image tags are all rejected
  ([architecture.md](architecture.md)); suite compilation locks fixture
  checksums and OCI digests so two compilations hash equally and mutated
  fixtures reject.
- Replay never "executes" a recording: playback is a pure projection
  (ADR-0005), and re-execution runs pinned code with fixtures, never the
  recording's tools (ADR-0001, ADR-0004).
- Browser surfaces apply CSP, CSRF and CORS controls plus escaping of
  untrusted payload rendering (boundary B2,
  [architecture.md](architecture.md)).

**Residual risk.** A novel parser bug in archive/YAML handling remains
possible; strict rejection and the adversarial corpus narrow but do not
eliminate it. Redaction may remove replay-critical evidence — surfaced as
a replay-ineligibility limitation rather than hidden.

**Detection.** Import/export and validation failures are explicit,
auditable errors; canonical hashes make tampering evident; round-trip
hashes must match on export/import.

### T3 — Secret and credential leakage (B1, B2, B4)

**Attack.** Secrets enter via prompts, tool arguments/results, environment
or errors, then leak into persisted events, exported bundles, logs, audit
trails, reports or the UI; provider credentials are stolen from the
platform or tricked into exfiltration via SSRF/redirects; a recording
deliberately seeded with secret-shaped canaries tests the pipeline.

**Mitigations.**

- Defense in depth redaction: SDK allowlists capture fields, scrubs
  credentials and applies configurable JSON pointers before
  buffering/transmission; the server applies a stricter second pass even
  for direct API uploads; redactions are recorded per event and
  replay-ineligibility is surfaced (ADR-0002, ADR-0003).
- Provider credentials exist only in the broker, in operator-controlled
  storage, scoped to allowlisted provider profiles; the broker refuses
  redirects to non-allowlisted hosts and never forwards runner-supplied
  URLs (ADR-0006). Live mode is opt-in per request and per profile.
- Auditing records reads, exports, executions and deletions without
  storing raw prompts or tokens in logs (ADR-0002, ADR-0006).
- Secret canaries are part of the test corpus: zero canaries may appear in
  persisted data, exported files, logs or reports
  ([architecture.md](architecture.md) testing gates). Release gates include
  secret detection on artifacts.
- Errors carry stable identifiers and actionable messages without echoing
  sensitive payloads (contract rule in [architecture.md](architecture.md)).

**Residual risk.** Redaction is pattern/pointer based: a secret in an
unexpected shape or field may survive until policy is updated; the
server-side pass and canary tests measure but cannot prove completeness.
Operators must treat recordings as sensitive regardless.

**Detection.** `redactions` arrays on events, audit events for
reads/exports, canary scans in CI, budget/cost accounting per brokered
call.

### T4 — Tenant isolation failures (B1, B5)

**Attack.** In a shared self-hosted deployment, a principal from project A
attempts to read, replay, export, delete or enumerate project B's runs,
recordings, keys or budgets — directly via IDs, via pagination/cursors,
via SSE streams, via job IDs, or by smuggling a cross-project reference
into an import.

**Mitigations.**

- Every project-owned row requires a project scope; application
  authorization and PostgreSQL row-level security enforce scope
  independently, so a bug in one layer does not expose data (ADR-0002,
  ADR-0003). Pooled connections reset session state so scope cannot leak
  between requests.
- API keys are scope-bound per project and per capability: capture keys
  cannot read or replay; replay permissions are separate; rotation and
  revocation take effect at the boundary (ADR-0002).
- Self-hosted deployments require TLS, OIDC-mapped identities, scoped
  keys, and hardened templates; local installs bind to loopback only
  (ADR-0002).
- Deletion and export jobs operate within the caller's project graph only;
  cross-project references are rejected with precise error codes
  (ADR-0003).
- Adversarial cross-project CRUD and worker tests are part of the test
  obligations for the persistence and API epics
  ([architecture.md](architecture.md)).

**Residual risk.** PostgreSQL superuser or a compromised API process can
cross boundaries — platform operators are trusted (see adversary model).
Cursor/SSE enumeration is mitigated by scope-checked queries but depends
on every new endpoint implementing scope checks; contract tests must cover
new endpoints.

**Detection.** Audit events per authenticated action; authorization
failures return stable problem identifiers that are countable and
alertable without payload logging.

### T5 — Replay-integrity attacks and judge manipulation (B2, B4)

**Attack.** Content inside a recording (tool results, prompts) carries
prompt-injection payloads targeting the re-executed agent or an optional
LLM judge; an attacker tries to make a regression look like a pass, to
make a judge override a deterministic failure, or to hide a fault that
never fired.

**Mitigations.**

- Injection payloads in fixtures can influence agent behavior inside the
  sandbox but cannot exfiltrate: no egress, no secrets, no tools
  (ADR-0004). Behavior changes they cause are exactly what comparison
  reports as divergence.
- Deterministic, judge and infrastructure statuses are independent;
  incomplete evidence or ambiguous alignment cannot pass; judges are off
  by default, schema-constrained, and can never override a deterministic
  or infrastructure failure (ADR-0005).
- Fault plans are versioned and validated; every applied or unreached
  fault is persisted as evidence, and unreached configured faults are
  reported rather than silently passing
  ([architecture.md](architecture.md)).
- Baseline updates create a new immutable recording and comparison, never
  overwrite existing evidence (ADR-0003).

**Residual risk.** A judge is a statistical component; its `pass` is
advisory within its rubric, which is why gating on judges is optional and
explicit.

**Detection.** `fault.applied` events, alignment-confidence reports
(`exact`, `partial`, `ambiguous`), judge provenance and uncertainty
fields, CI exit code 5 (`inconclusive`).

## 5. Errors, cancellation and resource cleanup

Cross-cutting operational rules (contract in
[architecture.md](architecture.md); per-area behavior in ADR-0001 through
ADR-0006):

- **Explicit failures.** Rejections use stable machine-readable problem
  identifiers (`application/problem+json`); overload yields `Retry-After`;
  asynchronous work returns `202` and terminal job state. No silent
  coercion of invalid input, and no failed capture may claim completeness.
- **Cancellation.** Jobs are cancellable (`POST /jobs/{id}:cancel`, CLI
  `anvil jobs cancel`); cancellation propagates to runners and broker
  reservations, targets termination within 10 seconds, and reports
  `infrastructure: cancelled` with evidence sealed up to that point.
- **Cleanup.** Every acquired resource — containers, leases, sockets,
  reservations, cursors — is released on success, error and cancellation;
  cleanup failures are reported, not swallowed. Crashed workers re-drive
  idempotent jobs (seal, export, deletion) without partial commits.
- **Auditability.** Failures and cancellations produce audit and evidence
  records without storing raw prompts or tokens.

## 6. Assumptions and open limitations

- The host running Anvil, its PostgreSQL, and platform operators are
  trusted; Anvil does not defend against host root.
- The runner contains project-trusted code with strong egress/secret
  denial, not a certified hostile-code sandbox (T1).
- Redaction reduces but cannot prove elimination of secrets (T3);
  recordings remain sensitive artifacts.
- Live-model mode concentrates risk in the broker by design; deployments
  that do not need it should not enable provider profiles at all.
- Performance targets in [architecture.md](architecture.md) are benchmark
  goals to be validated through ADR-tracked measurement, not achieved
  results.
