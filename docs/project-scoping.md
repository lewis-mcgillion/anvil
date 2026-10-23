# Project-scoped storage

Project-owned repository methods require a non-empty `projectID` and include it
in every mutation and lookup predicate. A missing scope fails before a query is
sent. A lookup, update or delete that is invisible to PostgreSQL row-level
security is reported as `storage: not found`; callers must not infer whether a
different project owns the identifier.

`storage.WithProject` establishes `anvil.project_id` with `SET LOCAL` inside a
transaction. The setting is therefore cleared by both commit and rollback
before a pooled connection can be reused. Callers must pass the request
context, so cancellation aborts the transaction; the helper rolls back any
transaction whose callback or commit fails. Worker code uses the same helper
and must not issue unscoped SQL.

The migration enables and forces RLS on every project-owned table. Direct SQL
without a transaction-local project setting returns no rows and cannot insert
or modify project-owned data.
