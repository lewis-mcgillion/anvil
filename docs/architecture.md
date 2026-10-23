# Anvil architecture and compatibility rules

Anvil is an open-source platform for agent reliability, replay, and regression
testing. This document is the baseline contract for implementations and
contributions.

## Contracts

Public schemas and APIs are contracts. Validate inputs at the boundary, return
stable machine-readable error identifiers, and document accepted values,
failure modes, and compatibility impact. Do not silently coerce invalid
input. A breaking change requires an explicit versioning decision and updated
schema and compatibility documentation.

## Security

Least privilege is the default. Secrets must not be written to logs, fixtures,
replay artifacts, or error messages. Validate untrusted input and make
authorization decisions at the boundary. Changes to authentication,
authorization, isolation, or tool execution require security-focused tests.

## Replay isolation

Replay is deterministic and must never execute production tools, make
production network calls, or mutate production state. Replays use recorded
inputs and explicitly controlled fakes or adapters. A missing or unsafe
adapter is an explicit failure, never a fallback to a live tool.

## Errors, cancellation, and cleanup

Errors are explicit and actionable; callers must be able to distinguish
rejection, cancellation, and internal failure. Cancellation stops new work and
propagates to child operations. Every acquired resource is released on
success, error, and cancellation, including when cleanup itself reports an
error.

## Versioning

Additive, backward-compatible fields and endpoints may be introduced without
changing existing meanings. Removing, renaming, tightening validation, or
changing error semantics is breaking and requires a version bump or an
announced migration path. Update public schemas and this compatibility
documentation with every interface change.
