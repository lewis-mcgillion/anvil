# Compatibility policy

Anvil's public contracts are:

- the REST API (`api/openapi.yaml`, base path `/v1`),
- the runner protocol (`proto/anvil/runner/v1/runner.proto`, over UDS),
- the event/recording JSON schemas (`schema_version`, ANVIL-003),
- the YAML suite format (`apiVersion: anvil.dev/v1alpha1`).

This policy governs how those contracts evolve. Python schema tests,
TypeScript code generation, and CI enforce the mechanical parts.

## Version identifiers

- REST: URI major version (`/v1`). The OpenAPI `info.version` is the
  document version of that major line.
- Runner protocol: protobuf package `anvil.runner.v1` plus the
  `protocol_version` string in `RunnerHello`. A supervisor rejects a hello
  whose major version it does not support; mismatch is fatal, not
  negotiated down silently.
- Events: `schema_version` per event (`"1"`). Projection: pinned
  `projection_version` query parameter.
- Suites: `apiVersion`/`kind` in the document.
- Pagination cursors: signed version `1` envelopes. Cursor contents are
  opaque and may change only in a backward-compatible way within `/v1`.

## Compatible (additive) changes — allowed within a major version

- New endpoints, new operations, new optional request fields.
- New optional protobuf fields with **new, never-before-used** field
  numbers; new messages; new enum values at the end.
- New response fields. Clients must ignore fields they do not know
  (proto3 unknown-field rules; JSON clients must not fail on extras).
- New error `code` values. Clients must treat unknown codes as generic
  failures of the HTTP status.
- New optional job outcome values and fields. Clients must treat
  `interrupted` as non-successful work and continue to handle unknown
  outcome values as an unspecified failure.

Consumers of enum-valued fields must handle unknown values as
`UNSPECIFIED`/unknown and fail closed, since a newer server may emit newer
values.

## Breaking changes — require a new major version (`/v2`, `anvil.runner.v2`)

- Removing or renaming endpoints, fields, messages, enums or enum values.
- **Renumbering or reusing protobuf field numbers.** Numbers are reserved
  forever once used; deleted fields must leave a `reserved` gap.
- Tightening validation of an existing field, changing a field's type or
  meaning, or changing default behavior (budgets, fixture matching,
  redaction).
- Changing error semantics for an existing `code`.
- Changing the scope required by an existing operation, except widening a
  previously over-privileged operation, which must be documented here.

The runner protobuf tag assignments are frozen in the protocol history.
Changes require explicit compatibility review and Python conformance tests,
plus an entry in this file.

## Authorization storage

Migration `0002_authorization` adds project-scoped API keys. Only a SHA-256
hash is persisted; the generated bearer secret is returned once. Keys may be
rotated by creating a replacement with `rotated_from`, and revocation is a
timestamped state transition. The migration rejects unknown scopes and
prevents capture keys from being combined with read or replay access.

## Process for contract changes

1. Update `api/openapi.yaml` and/or `proto/anvil/runner/v1/runner.proto`.
2. Regenerate TypeScript clients using `make generate`. Generated types must compile.
3. Run the Python schema suite and TypeScript contract tests. Check runner
   protobuf breaking changes before publishing an altered protocol.
4. Update `docs/api-contract.md` and this document in the same change.
5. Contract changes ship with fixtures or conformance coverage (ANVIL-006)
   before dependent features land.

The PostgreSQL job lease store is introduced by the additive
`migrations/0002_jobs.sql` migration. It records attempts and interrupted
worker outcomes without rewriting existing event or recording data.

Migration `0003_run_metadata.sql` adds nullable columns for capture agent,
adapter, adapter version and trace identifiers. Old runs remain readable;
new capture requests persist these fields. Apply migrations in order:
`0001_core`, `0002_authorization`, `0002_jobs`,
`0002_administration`, then `0003_run_metadata`.
Apply `0004_seal_jobs` afterwards to enable durable asynchronous sealing.
Apply `0005_agent_versions` next to register digest-pinned OCI agent images.
Agent versions are project-scoped and enforce the immutable SHA-256 image
reference format in PostgreSQL. The registration endpoint does not execute
images or grant execution permissions. Any future replay runner must enforce
isolation and fixture requirements independently before launching a workload.

## Sunset and migration

When a `/v2` line is introduced, `/v1` remains available for at least one
milestone with identical semantics for immutable artifacts (recordings,
manifests). Migrations of stored data are explicit, tested forwards and
documented per release; immutable recordings are never rewritten in place
— a new projection creates new evidence.
