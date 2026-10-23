# ADR-0006: Model broker and credential isolation

- Status: Accepted
- Date: 2026-10-23
- Tracking: ANVIL-002

## Context

Live-model re-execution needs fresh inference, but model provider
credentials are the most dangerous secret Anvil touches: a leak or an
SSRF-style redirect could spend money or exfiltrate prompts.
[docs/architecture.md](../architecture.md) requires that only one component
may access explicitly configured model endpoints and that live inference is
opt-in. This ADR concentrates all live-model risk in a single, auditable
broker and defines its controls.

## Decision

1. **The broker is the only egress point for model traffic.** Runners have
   no network (ADR-0004); the API and workers hold no provider credentials.
   Only the broker can reach model endpoints, and only those on an
   allowlisted provider profile.
2. **Credentials are server-managed and profile-scoped.** A provider
   profile defines allowed endpoints, models, budgets and the credentials
   (kept in operator-controlled storage). Live mode requires an explicit
   `model_mode: live` request referencing an approved profile; nothing
   defaults to live.
3. **Hard budgets with enforced cancellation.** Default limits: 120-second
   wall time, 20 model calls, 100 tool calls, 20k input and 10k output
   tokens, USD 1.00 estimated model spend per re-execution. Budgets are
   enforced before dispatch (pre-flight estimate) and accounted after
   dispatch; exceeding any budget halts the run as a budget failure, not a
   pass.
4. **SSRF and redirect protections.** The broker validates endpoint URLs
   against the profile allowlist, refuses redirects to non-allowlisted
   hosts, and never forwards runner-supplied URLs. Provider responses are
   treated as untrusted data (boundary B2/B4, ADR-0002).
5. **Auditable.** Every brokered call emits an audit event with profile,
   model, token/cost accounting and outcome — without storing raw prompts
   in logs.
6. **Throttling and back-pressure.** Overload returns `Retry-After`;
   callers (SDK/exporter and job orchestrator) back off rather than
   dropping or silently succeeding.

## Consequences

- There is exactly one component to harden, audit and (if desired) deploy
  with stricter network policy for live inference; recorded modes require
  no credentials at all.
- Budget and allowlist enforcement make "runaway agent spends money" and
  "fixture smuggles an exfiltration URL" explicit failures.
- Because live mode is opt-in per request and per profile, CI defaults to
  recorded/stub providers and never depends on flaky live models as
  deterministic oracles.
- Token/cost accounting per run feeds divergence reports (budget diffs)
  and project quotas.

## Alternatives considered

- **Runner-direct provider access with an egress allowlist.** Rejected:
  spreads credentials and SSRF surface into every sandbox; the broker
  concentrates both.
- **Per-request user-supplied credentials.** Rejected: user secrets in
  request payloads would land in logs, audits and storage; only
  operator-managed profile credentials are used.
- **Unlimited live mode behind a warning.** Rejected: warnings are not
  controls; hard budgets and explicit opt-in are.

## Errors, cancellation and cleanup

- Disallowed endpoint, unknown profile, redirect to a non-allowlisted host
  or budget exhaustion each fail with a stable problem identifier; no
  request is dispatched on failure.
- Cancelling a live re-execution revokes the broker reservation, aborts
  in-flight provider calls best-effort, and reports
  `infrastructure: cancelled`; token/cost usage up to cancellation is
  still accounted as evidence.
- On broker failure or unavailability, live-mode jobs fail closed as
  infrastructure errors; recorded-mode replay is unaffected and never
  "upgrades" to live to compensate.
- Credentials are never written to logs, fixtures, recordings, exports or
  error messages; rotation updates profiles without touching stored
  recordings.
