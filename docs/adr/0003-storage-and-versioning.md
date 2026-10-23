# ADR-0003: Storage model, event immutability and versioning

- Status: Accepted
- Date: 2026-10-23
- Tracking: ANVIL-002

## Context

Everything in Anvil — playback determinism, divergence analysis, audit and
the replay invariant — depends on recorded evidence being trustworthy. The
store must make tampering obvious, retries harmless, cross-tenant access
impossible and schema evolution explicit.
[docs/architecture.md](../architecture.md) ("Storage and versioning")
specifies the entity set and event envelope; this ADR fixes the storage
model and its invariants.

## Decision

1. **Single authoritative store.** PostgreSQL holds all metadata, immutable
   event payloads (JSONB), artifacts, suites, job leases and audit events.
   There is no Redis and no separate object store for MVP.
2. **Events are immutable and idempotent.** An accepted event is never
   updated or deleted (except via documented retention/deletion workflows).
   Events are idempotent by event identity plus content hash: a duplicate
   retry is a no-op, while conflicting content for an existing identity or
   sequence returns HTTP 409 with no partial insertion of the batch.
3. **Sequences are per stream.** There is no fictitious global total order.
   Causal parents may arrive after their children; sealing is what validates
   causal completeness. Source timestamps are never used as a deterministic
   order (see ADR-0005).
4. **Completeness is separated from outcome.** Capture completeness
   (`open`, `sealing`, `sealed_complete`, `sealed_incomplete`) is tracked
   independently of execution outcome (`running`, `succeeded`, `failed`,
   `cancelled`, `unknown`). A run may be a complete recording of a failed
   agent, or an incomplete recording of a succeeded one; neither may be
   presented as something it is not.
5. **Every entity is project-scoped and doubly enforced.** All
   project-owned rows carry a project scope. Application authorization and
   PostgreSQL row-level security reinforce each other; pooled connections
   reset session state so scope never leaks across requests.
6. **Versioned envelopes.** Events carry `schema_version`; recordings carry
   a manifest with schema, projection and adapter versions plus SHA-256
   hashes of artifacts. Identifiers are UUIDv7; original OpenTelemetry
   trace/span IDs are preserved separately. No pickle or arbitrary object
   deserialization is accepted from recordings.
7. **Bounded by default.** Limits (max event 256 KiB, ingest batch 4 MiB or
   500 events, artifact 8 MiB, run 64 MiB, SDK buffer 2000 events or
   16 MiB, 30-day retention, page size 100/max 1000) are enforced at
   admission; failed capture must report gaps and never silently succeed.
8. **Migrations are explicit and tested.** Schema evolution requires
   forward migrations plus prior-version upgrade tests. Removing, renaming
   or tightening a contract is breaking and follows the versioning rule in
   [docs/architecture.md](../architecture.md).

## Consequences

- Playback and comparison can treat stored events as ground truth; any
  tampering breaks content hashes and manifest hashes.
- SDK retries under flaky networks cannot corrupt a run; clients can safely
  resend batches.
- "Incomplete" is a first-class, honest state: gaps are recorded as
  `capture.gap` events and surfaced in eligibility instead of being hidden.
- A single database simplifies backup/restore, retention and deletion
  semantics — deletion is graph traversal over one store with audit
  evidence, and backups follow documented retention.
- Query patterns that need cross-tenant access (none in the public API)
  would have to bypass RLS deliberately, which is auditable.

## Alternatives considered

- **Append-only object storage for events with a metadata index.**
  Rejected for MVP: a second store doubles backup, consistency and deletion
  complexity; JSONB in PostgreSQL meets the ingestion targets with simpler
  transactional guarantees.
- **Mutable "latest state" rows updated in place.** Rejected: projection
  must be a pure function of immutable events (ADR-0005); mutable evidence
  would make canonical projection hashes meaningless.
- **Global monotonically increasing sequence.** Rejected: capture is
  concurrent across streams; a global order would force false coordination
  and still not represent causality.

## Errors, cancellation and cleanup

- Conflicting event identity/sequence returns HTTP 409 with a stable
  problem identifier; the batch is rejected atomically (no partial commit).
- Oversized events, batches or artifacts are rejected at admission with
  `Retry-After` where overload-related; the SDK records a `capture.gap`
  rather than claiming completeness.
- Sealing is idempotent and crash-safe: a seal interrupted mid-flight can
  be re-driven; the final recording manifest is written once, immutably.
- Deletion and retention jobs run as cancellable jobs with audit evidence;
  cancelling leaves already-committed deletions committed and remaining
  work resumable — never half-committed rows.
