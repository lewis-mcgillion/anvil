# Architecture decision records

This directory records the binding architecture decisions for Anvil. ADRs
implement the contract, security and versioning rules in
[../architecture.md](../architecture.md) and provide the decision-level
evidence for the [threat model](../threat-model.md).

## Rules

- ADRs are numbered sequentially (`NNNN-title.md`) and are immutable once
  Accepted, except for typo fixes that change no meaning.
- A decision is reversed only by a new ADR that marks the old one
  `Superseded by ADR-NNNN`. Never silently edit an accepted decision.
- Every ADR states its context, the decision, consequences, rejected
  alternatives, and error/cancellation/cleanup behavior where the decision
  involves operations that can fail, be cancelled or hold resources.
- An ADR that changes a public contract (API, schema, protocol, suite format)
  must say so explicitly and reference the version bump or migration path,
  per the versioning rule in [../architecture.md](../architecture.md).
- No ADR may weaken the replay invariant: replay never executes production
  tools, makes production network calls or mutates production state.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-execution-modes.md) | Execution modes and fail-closed semantics | Accepted |
| [0002](0002-trust-boundaries.md) | Trust boundaries and authorization | Accepted |
| [0003](0003-storage-and-versioning.md) | Storage model, event immutability and versioning | Accepted |
| [0004](0004-runner-isolation.md) | Runner isolation and supervisor boundary | Accepted |
| [0005](0005-replay-and-fixture-matching.md) | Replay determinism and strict fixture matching | Accepted |
| [0006](0006-model-broker.md) | Model broker and credential isolation | Accepted |
