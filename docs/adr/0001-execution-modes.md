# ADR-0001: Execution modes and fail-closed semantics

- Status: Accepted
- Date: 2026-10-23
- Tracking: ANVIL-002

## Context

Anvil must let users inspect a captured run and reproduce a failure without
ever touching production systems, while still allowing an explicit, opt-in
path to fresh model inference. [docs/architecture.md](../architecture.md)
("Execution semantics") defines the guarantee matrix; this ADR makes the
mode split and the fail-closed rule a binding decision.

The forces at play:

- A user inspecting a recording must get a deterministic answer with zero
  code execution, so playback can be trusted on any machine.
- A user reproducing a failure wants to run changed agent code against the
  recorded tool/model responses, with no risk of side effects.
- A user validating against a new model wants fresh inference, but only
  through a controlled, credentialed, budgeted path.
- Any ambiguity (missing fixture, changed prompt, ambiguous match) must be a
  loud failure, never a silent fallback to production.

## Decision

Anvil has exactly three execution modes. There is no fourth mode and no
implicit transition between modes.

| Mode | Agent code | Model calls | Original tools | Guarantee |
|---|---|---|---|---|
| Recorded playback | Never | Never | Never | Deterministic projection of immutable stored events under a pinned schema/projection version |
| Recorded re-execution | Yes, sandboxed | No | Never | Recorded model and tool responses supplied through checked fixtures; execution itself need not be deterministic |
| Live-model re-execution | Yes, sandboxed | Only through an opt-in broker | Never | Fresh model inference with mocked tools; responses and execution may vary |

1. **Recorded playback is a pure projection.** It reads immutable events and
   renders them under a pinned `schema_version`/`projection` version. It
   schedules no jobs, starts no runner and issues zero broker calls.
2. **Recorded re-execution** runs agent code inside the isolated runner
   (ADR-0004) with every model and tool interaction served from checked
   fixtures (ADR-0005). The original tools are never present in the runner.
3. **Live-model re-execution** is identical except model requests are routed
   to the broker (ADR-0006). It requires an explicit `model_mode: live`
   request plus an approved provider profile. Tool invocations still come
   only from fixtures; live mode changes model provenance, never tool
   provenance.
4. **Fail closed.** Any unmatched or ambiguous tool invocation or model
   request aborts the run with an explicit mismatch error. No mode silently
   falls back to production systems, and no recorded model response is
   served without an exact canonical request match.
5. **Mode is explicit input, never inferred.** Every re-execution request
   carries `model_mode` (`recorded` or `live`); UIs and CLI must label the
   mode in output and reports.

## Consequences

- Users can reason about safety per mode: playback and recorded re-execution
  require no credentials and no network; live re-execution concentrates all
  credential risk in one auditable component.
- Intentionally changing a prompt invalidates recorded model fixtures; the
  user must supply a new fixture or opt into live mode. This is a feature:
  it prevents accidental "replay drift" from masquerading as a pass.
- The runner never needs a "tool escape hatch", which keeps the runner
  protocol free of any arbitrary-execution primitive.
- New capabilities must pick one of the three modes; proposing a mode with
  weaker isolation is a breaking change requiring a superseding ADR.

## Alternatives considered

- **Single "replay" mode with flags.** Rejected: conflating projection,
  recorded re-execution and live inference makes the safety guarantees
  unreadable and invites accidental live execution.
- **Automatic fallback to live tools/models when a fixture is missing.**
  Rejected absolutely: it violates the replay invariant that replay never
  executes production tools.
- **Best-effort (fuzzy) fixture matching.** Rejected: near-matches hide
  prompt or argument changes that are exactly what regression testing must
  catch; see ADR-0005.

## Errors, cancellation and cleanup

- Mode selection is validated at request admission; an unknown or
  disallowed mode is rejected with `application/problem+json` before any
  job is created.
- A fixture mismatch or ambiguity fails the run with a structured
  `mismatch`/`ambiguous` outcome recorded as evidence; the candidate run is
  still sealed so the failure is inspectable.
- Cancelling a re-execution stops the runner within the documented bound
  (termination within 10 seconds target) and releases the job lease, runner
  sandbox and any broker reservations; a cancelled run reports
  `infrastructure: cancelled`, never `pass`.
