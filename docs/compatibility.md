# Compatibility policy

Anvil's public contracts are:

- the REST API (`api/openapi.yaml`, base path `/v1`),
- the runner protocol (`proto/anvil/runner/v1/runner.proto`, over UDS),
- the event/recording JSON schemas (`schema_version`, ANVIL-003),
- the YAML suite format (`apiVersion: anvil.dev/v1alpha1`).

This policy governs how those contracts evolve. Contract tests in
`internal/contracts` and CI (`buf lint`, spec validation, generated-code
builds, field-tag goldens) enforce the mechanical parts.

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

## Compatible (additive) changes — allowed within a major version

- New endpoints, new operations, new optional request fields.
- New optional protobuf fields with **new, never-before-used** field
  numbers; new messages; new enum values at the end.
- New response fields. Clients must ignore fields they do not know
  (proto3 unknown-field rules; JSON clients must not fail on extras).
- New error `code` values. Clients must treat unknown codes as generic
  failures of the HTTP status.

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

The golden file `internal/contracts/testdata/runner_proto_tags.golden`
pins every protobuf (message, field, number) triple; any renumber or
removal fails CI. Additive changes require regenerating the golden with
`UPDATE_GOLDEN=1 go test ./internal/contracts/...` in the same PR as the
proto change, plus an entry in this file.

## Authorization storage

Migration `0002_authorization` adds project-scoped API keys. Only a SHA-256
hash is persisted; the generated bearer secret is returned once. Keys may be
rotated by creating a replacement with `rotated_from`, and revocation is a
timestamped state transition. The migration rejects unknown scopes and
prevents capture keys from being combined with read or replay access.

## Process for contract changes

1. Update `api/openapi.yaml` and/or `proto/anvil/runner/v1/runner.proto`.
2. Regenerate clients: `make generate` (Go types/client, Go protobuf,
   TypeScript types). Generated code must compile.
3. Run `buf lint`, `buf breaking` (against `main`), and
   `go test ./internal/contracts/...`.
4. Update `docs/api-contract.md` and this document in the same change.
5. Contract changes ship with fixtures or conformance coverage (ANVIL-006)
   before dependent features land.

## Sunset and migration

When a `/v2` line is introduced, `/v1` remains available for at least one
milestone with identical semantics for immutable artifacts (recordings,
manifests). Migrations of stored data are explicit, tested forwards and
documented per release; immutable recordings are never rewritten in place
— a new projection creates new evidence.
