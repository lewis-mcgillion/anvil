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
For a disposable database restore, keep credentials outside command history
and use separate connection URLs supplied through environment files or a
secret manager. A typical logical-backup sequence is:

```sh
pg_dump --format=custom --no-owner --no-acl --file=anvil.backup "$ANVIL_SOURCE_DATABASE_URL"
createdb --maintenance-db="$ANVIL_ADMIN_DATABASE_URL" anvil_restore
pg_restore --exit-on-error --no-owner --no-acl --dbname="$ANVIL_RESTORE_DATABASE_URL" anvil.backup
```

These commands require a **new, empty, disposable** restore database with
matching PostgreSQL major version and installed extensions. Never point
`ANVIL_RESTORE_DATABASE_URL` at production. Do not automatically reapply
migrations already included in a logical backup: first inspect the restored
schema and migration history. The `migrations/` directory contains several
`0002_*.sql` files; numeric prefixes alone do not establish a safe upgrade
order. Confirm the applied migration set and dependencies with the release
operator before running any SQL. `pg_restore` success alone does not prove
recording integrity, tenant isolation, RLS or suite replay correctness.
Verify project RLS and authorization before pointing an API at restored data.
Do not downgrade schema versions in place; use a verified backup/restore
procedure. Run recorded fixture tests and compare immutable projection hashes
before reopening any services. Full restoration and suite replay instructions
remain pending ANVIL-076 and ANVIL-058.

Current limitation: there is no supported production installation, verified
isolated runner or live broker. This document does not replace independent
restore validation or authorize a public deployment.
