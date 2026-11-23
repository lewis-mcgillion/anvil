# Anvil operator and contributor preflight

## Contributor workflow

Use Python 3.12 with the pinned dependencies and work in a dedicated branch.
Run focused tests for each changed component before opening a PR. Keep replay
strictly offline: the fixture matcher must never execute original tools,
contact providers, or treat unavailable evidence as success. Review
`docs/architecture.md`, `docs/threat-model.md` and `docs/adr/` before changing
public contracts. Interface changes require schema and compatibility tests.

## Operator checklist

- Bind the API to loopback during development; public deployments require TLS,
  trusted OIDC claims and operator-controlled secrets. Never mount secrets or
  the Docker socket into a runner.
- Validate and apply PostgreSQL migrations in version order in a disposable
  instance before upgrading production. Create and verify a backup before
  schema changes. Preserve immutable recording evidence and hashes.
- Treat 429 as throttling: honor `Retry-After` and retry only safe/idempotent
  operations with the original idempotency key. Cancellation must fence the
  worker and leave a terminal, auditable outcome.
- On a suspected leak, disable external access, rotate affected API keys and
  provider profiles, preserve redacted audit evidence and restore service
  only after project-boundary validation.

## Recovery and upgrades

Restore PostgreSQL into an isolated environment first, replay the forward
migrations and compare recording archive hashes with the backed-up manifest.
Verify project RLS and authorization before pointing an API at restored data.
Do not downgrade schema versions in place; use a verified backup/restore
procedure. Run recorded fixture tests and compare immutable projection hashes
before reopening any services. Full restoration and suite replay instructions
remain pending ANVIL-076 and ANVIL-058.

Current limitation: there is no supported production installation, verified
isolated runner or live broker. This document does not replace independent
restore validation or authorize a public deployment.
