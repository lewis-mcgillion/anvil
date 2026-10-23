# ADR-0004: Runner isolation and supervisor boundary

- Status: Accepted
- Date: 2026-10-23
- Tracking: ANVIL-002

## Context

Recorded and live re-execution run agent code. That code is project code
(not arbitrary hostile code), but it is still code the platform did not
write, executing against fixtures that may contain secrets-shaped strings
and prompt-injection payloads. The platform must guarantee that running an
agent can never touch the network, production credentials, the host
filesystem or other tenants — even if the agent or a malicious recording
tries. [docs/architecture.md](../architecture.md) ("Stack, layout and
dependencies", "Security and privacy") sets the requirements; this ADR
fixes the isolation mechanism and the supervisor protocol.

## Decision

1. **OCI-digest-pinned runner container.** The agent runs in a container
   referenced by immutable digest (mutable tags are rejected). The
   environment has:
   - no network interface access (egress denied);
   - no production secrets or environment variables;
   - no host directory mounts and no Docker socket;
   - resource limits (CPU, memory, process count) with enforced budgets.
2. **A supervisor mediates everything.** The runner speaks only a narrow,
   versioned protobuf protocol over a Unix domain socket to the supervisor:
   `RunnerHello`, `EmitEvents`, `ModelRequest`, `ModelResponse`,
   `ToolRequest`, `ToolResponse`, `Heartbeat`, `RunnerFinished`. The
   protocol deliberately exposes **no** arbitrary HTTP, filesystem, command
   execution or original-tool invocation primitive.
3. **All effects are decided outside the sandbox.** A `ToolRequest` is
   answered from fixtures by the supervisor-side matcher (ADR-0005); a
   `ModelRequest` is answered from fixtures or, in live mode, forwarded to
   the broker (ADR-0006). The runner itself can initiate nothing.
4. **Fail closed.** If isolation enforcement (seccomp, egress deny,
   namespace setup, UDS permissions) is unavailable or cannot be verified,
   the job fails as an infrastructure error instead of running permissively.
5. **Provenance is recorded.** Every run seals a candidate manifest with
   image digest, adapter and version, fixtures, model mode, fault plan,
   budgets and outcomes, so a result can be traced to exactly what ran.
6. **Honest scope.** The runner is trusted-project-code isolation with
   strong egress/secret denial. It is not claimed to be a hardened
   multi-tenant sandbox for deliberately malicious code (see
   [threat model](../threat-model.md)); stronger sandboxing is a future,
   separately decided option.

## Consequences

- Production-tool execution during replay is architecturally impossible,
  not just policy-prohibited: the runner has no credentials, no network and
  no protocol message that could reach a production tool.
- A prompt-injection payload inside a fixture can influence agent behavior
  (that is observable and comparable) but cannot exfiltrate anything:
  there is no egress and no secret to read.
- Supervisor-mediated effects make every tool/model interaction
  interceptable, budgetable and auditable in one place.
- Image-digest pinning plus the registry (approved digests, entrypoints,
  lock metadata) makes "what ran" reproducible and rejects drift.

## Alternatives considered

- **Run agent code in-process with the worker.** Rejected: any SDK or agent
  bug would have worker privileges, database access and network.
- **Allowlist-based network egress from the runner.** Rejected for
  recorded modes: recorded re-execution needs no network at all, so the
  safest allowlist is "deny all". Live mode egress lives solely in the
  broker (ADR-0006), not in the runner.
- **gVisor/Firecracker microVMs.** Deferred: meaningful hardening against
  hostile code, but heavier to operate for local-first MVP; recorded-mode
  threat model does not require it. Revisit when untrusted multi-tenant
  execution becomes a goal; that change requires a superseding ADR.

## Errors, cancellation and cleanup

- Runner startup, heartbeat loss, budget exhaustion and protocol violations
  produce `infrastructure` outcomes (`error` or `timed_out`) that never
  read as passes; the candidate run is sealed with the failure evidence.
- Cancellation (`POST /jobs/{id}:cancel` or CLI `anvil jobs cancel`) is
  delivered to the supervisor, which terminates the runner within the
  documented bound (target: termination within 10 seconds) and reports
  `infrastructure: cancelled`.
- On every exit path — success, error, timeout, cancellation — the
  supervisor destroys the container, releases CPU/memory/lease accounting,
  closes the UDS, and seals whatever evidence exists. Cleanup failure is
  itself reported rather than silently leaking resources.
