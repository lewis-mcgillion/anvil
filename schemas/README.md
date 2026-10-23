# Anvil event and recording schemas

This directory contains the frozen, versioned JSON Schemas for Anvil's event and
recording contract, as required by `docs/architecture.md` ("Storage and
versioning", "Replay, comparison and fault design"). The Go API/worker
(`internal/events`), the Python capture SDK (`sdk/python/anvil_schema`) and the
TypeScript web code (`web/schemas`) all validate against these exact documents;
the shared fixture corpus in `fixtures/schema/v1/` proves the three validators
agree on positive and negative cases.

## Layout

Every schema under `v1/` is JSON Schema draft 2020-12 and carries a stable
`$id` under `https://anvil.dev/schemas/v1/`:

| Schema | Covers |
|---|---|
| `defs.schema.json` | Shared scalars: `schema_version`, UUIDv7, OTel trace/span IDs, timestamps, SHA-256, JSON Pointers, event types, completeness/outcome enums |
| `event.schema.json` | The immutable event envelope and per-type payloads (`run.started` … `fault.applied`) |
| `run.schema.json` | Run metadata; capture completeness is separate from execution outcome |
| `stream.schema.json` | Per-stream ordering metadata (sequences are per stream, never a global order) |
| `artifact.schema.json` | Bounded, SHA-256-hashed artifact metadata |
| `capture-gap.schema.json` | Explicit capture-gap records — failed capture reports gaps, never silently succeeds |
| `seal.schema.json` | Seal request; `acknowledge_incomplete` gates sealing runs with known gaps |
| `manifest.schema.json` | Immutable recording manifest, including re-execution provenance (image digest, adapter, fixtures, model mode, fault plan, budgets) |

## Versioning and compatibility

- `schema_version` is pinned to `"1"` everywhere. Any other value MUST be
  rejected with an explicit validation error; validators must never guess.
- Schemas are **closed**: `additionalProperties: false` on every defined
  object, and the event-type and state enums are exhaustive. Unknown fields and
  unknown event types fail validation rather than being silently dropped.
- Evolution rules (per `docs/architecture.md`: "Schema evolution requires
  explicit compatibility and migrations"):
  - Additive, optional-only changes may remain in `v1` but require updating
    `fixtures/schema/v1/` and this document in the same change.
  - Any breaking change (removed/renamed field, tightened constraint, new
    required field, new enum semantics) requires a new `schemas/v2/` directory,
    a new `schema_version` value and a documented migration path.
  - A baseline update creates a new immutable recording; existing recordings
    and their manifests are never overwritten.
- Size limits (event 256 KiB, ingest batch 4 MiB / 500 events, artifact 8 MiB,
  run 64 MiB) are enforced by the ingest path, not by these schemas; the
  schemas document the shapes only.

## Replay safety invariant

These schemas only *describe* captured data. Recorded playback projects stored
events under this pinned schema version and never executes agent code, model
calls or original tools. The `manifest.schema.json` re-execution provenance
records fixture policy (`strict`), model mode and budgets so that re-execution
evidence is auditable; nothing in this contract permits a replay path to invoke
production tools.

## Validation errors

Each language validator raises/returns an explicit error naming the schema id
and the failing instance location; fixtures ending in `.invalid.json` are the
negative contract and must fail in all three languages. Resource cleanup is
trivial by design: validation is pure and in-memory, and compiled schemas are
immutable and safe to cache and share.
