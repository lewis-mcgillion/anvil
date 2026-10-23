# ADR-0005: Replay determinism and strict fixture matching

- Status: Accepted
- Date: 2026-10-23
- Tracking: ANVIL-002

## Context

Replay is Anvil's core promise: given a recording, produce the same answer
every time, and when re-executing an agent, detect — loudly — any drift in
what the agent asks of its tools or model. This requires two things: a
canonical, stable representation of recorded data, and a matching rule that
never guesses. [docs/architecture.md](../architecture.md) ("Replay,
comparison and fault design") specifies the mechanics; this ADR fixes the
determinism contract and the fail-closed matching rule that preserves the
no-production-tool-execution invariant.

## Decision

1. **Canonicalization is RFC 8785 (JCS).** All comparison and fixture
   matching operates on RFC 8785 JSON canonicalization with explicit,
   schema-safe handling of numbers and strings. The same normalization runs
   in every implementation language, verified by cross-language fixtures.
2. **Projection is a pure function.** Playback projects immutable events
   into read-only state under a pinned `projection` version. The same
   recording plus the same projection version yields the same canonical
   projection hash on every platform. Read-only playback never schedules
   execution (ADR-0001).
3. **Causal order, not wall-clock.** Projection order derives from causal
   parent relationships with stable tie-breaks. Source event timestamps are
   never used as a deterministic order. Gaps and cycles are flagged, not
   silently ordered around.
4. **Strict fixture matching.** A recorded response is served only when all
   of the following match:
   - logical invocation path (the causal identity of the call site);
   - tool name and schema digest;
   - canonical arguments (RFC 8785);
   - consumption state (each fixture is consumed in order, once).
5. **Ambiguity is an error.** Parallel identical calls must carry distinct
   causal branch identities; if they cannot be distinguished, matching
   fails with an explicit ambiguity error rather than picking one.
6. **Model fixtures are exact.** A recorded model response requires the
   canonical messages, tools, system instructions and generation settings
   to match exactly; any change is an explicit mismatch, never a
   near-match.
7. **Comparison is honest about evidence.** Deterministic, judge and
   infrastructure statuses are reported independently; incomplete evidence
   or ambiguous alignment cannot produce a `pass`. Optional LLM judges are
   off by default and can never override a deterministic or infrastructure
   failure.

## Consequences

- Replaying the same recording is bit-stable, enabling golden-hash
  conformance testing across OS/architecture.
- A developer who changes a prompt, reorders parallel calls or alters an
  argument gets an immediate, explainable failure with evidence — the
  regression signal the platform exists for.
- Because matching fails closed, there is no code path in which an
  unmatched tool call "just runs" against production; the invariant
  survives bugs in the matcher.
- Pinning the projection version means schema/projection evolution is
  explicit and versioned per the compatibility rules, rather than a silent
  behavior change.

## Alternatives considered

- **Fuzzy/approximate argument matching.** Rejected: silent near-matches
  destroy regression signal and can mask genuine behavioral changes.
- **Timestamp-ordered replay.** Rejected: capture is concurrent and clocks
  are untrustworthy; causality is the only honest order, with timestamps
  reserved for presentation.
- **Mutable projections updated as new events arrive.** Rejected: a pure,
  pinned projection of immutable events (ADR-0003) is what makes repeated
  runs comparable.

## Errors, cancellation and cleanup

- Fixture miss, reordered consumption, repeated consumption, changed
  arguments or ambiguous identity each produce a distinct, stable error
  kind recorded as run evidence; the run fails, it does not degrade.
- Playback of a recording with gaps or a causality cycle flags the affected
  region and reports capture incompleteness instead of inventing an order.
- Seeking and checkpoint restores are read-only; a failed seek reports an
  error and leaves no partial state. Cancellation of a playback or
  comparison job frees its caches and cursors and records
  `infrastructure: cancelled`.
