# ADR-0002: Trust boundaries and authorization

- Status: Accepted
- Date: 2026-10-23
- Tracking: ANVIL-002

## Context

Anvil handles prompts, tool arguments, tool results and recordings, all of
which may contain secrets or personal data. It runs agent code, renders
untrusted content in a browser, and (in live mode) holds model credentials.
[docs/architecture.md](../architecture.md) ("Security and privacy") requires
least privilege, boundary validation and defense in depth; this ADR fixes
where the trust boundaries are and who may cross them.

## Decision

Anvil recognizes five trust domains, each with a hard boundary:

| Boundary | Untrusted side | Trusted side | Crossing mechanism |
|---|---|---|---|
| B1 Capture | Instrumented agent process, network clients | Anvil API | Scope-bound API keys, strict request decoding, server-side second-pass redaction |
| B2 Data | Stored events, recordings, archives, Markdown, judge input, agent output | Projection, rendering, comparison | Treat all stored content as untrusted data; validate against versioned schemas; escape on render |
| B3 Execution | Agent code in the runner | Supervisor, host | Isolated OCI runner with no network/secrets/mounts; narrow versioned protobuf over Unix domain socket (ADR-0004) |
| B4 Model egress | Runner, API, workers | Model broker, providers | Only the broker holds provider credentials; allowlisted endpoints, budgets, SSRF protection (ADR-0006) |
| B5 Tenancy | Other projects/organizations | Project data | Application authorization plus PostgreSQL row-level security, reinforced independently (ADR-0003) |

Rules:

1. **Authorization is decided at the boundary.** API keys are scoped
   (capture keys cannot read or replay; replay permissions are separate).
   Self-hosted deployments add OIDC-authenticated principals mapped to
   project grants. Local installs bind to loopback only.
2. **Everything persisted is untrusted when read back.** Traces, Markdown,
   imported archives, judge input and agent output are rendered, parsed or
   projected with the same suspicion as fresh network input. No
   agent-provided instructions can alter permissions or runner controls.
3. **No component holds credentials it does not need.** The runner holds
   none; the API holds none for model providers; only the broker does.
4. **Fail closed at every boundary.** If redaction policy, authorization
   policy or isolation enforcement is unavailable, the operation fails
   rather than degrading to a permissive path.

## Consequences

- A compromise of the agent process, a recording, or even agent code running
  in the runner cannot by itself reach project data of another tenant,
  provider credentials or production tools.
- Redaction happens twice (SDK pre-export, then a stricter server pass), so
  a client bug cannot put canary secrets into storage unchallenged.
- Browser rendering must escape all recorded content; a recording is an XSS
  vector until proven otherwise (see the threat model).
- Auditing focuses on boundary crossings: reads, exports, executions and
  deletions are audit events, without storing raw prompts in logs.

## Alternatives considered

- **Trust the SDK's redaction only.** Rejected: direct API uploads bypass
  the SDK; the server applies a stricter second pass regardless of source.
- **Single global authorization check in the API layer.** Rejected:
  application authorization and PostgreSQL row-level security are layered so
  a bug in one does not expose another project.
- **Treat runner agent code as fully untrusted (gVisor/Firecracker class
  sandbox) for MVP.** Deferred: agent code in a local container is trusted
  project code per [docs/architecture.md](../architecture.md); the runner is
  hardened against escapes and egress, but it is not marketed as a hardened
  multi-tenant untrusted-code sandbox. This limitation is stated openly
  rather than implied.

## Errors, cancellation and cleanup

- Authentication/authorization failures return stable problem identifiers
  (`application/problem+json`) and are auditable without logging payloads.
- When enforcement components (redaction policy, RLS context, seccomp/egress
  setup) cannot be initialized, the corresponding endpoint or job fails
  closed and reports an infrastructure error.
- Key rotation and revocation take effect at the boundary immediately;
  in-flight jobs keep their already-admitted scope until completion or
  cancellation, and their resource cleanup follows ADR-0001/ADR-0004.
