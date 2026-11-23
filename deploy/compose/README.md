# Local-only Compose foundation

Set `ANVIL_POSTGRES_PASSWORD` to a URL-safe strong value and
`ANVIL_CURSOR_SECRET` to a distinct random value of at least 16 characters.
From `deploy/compose`, run `docker compose up --build`. API access is bound to
`127.0.0.1:8080`, and PostgreSQL is not published to the host.

This is **not yet** a complete Anvil installation. Run SQL migrations in
version order before using API persistence. The worker requires an explicitly
configured project ID (`ANVIL_WORKER_PROJECT_ID`) and is opt-in via
`docker compose --profile worker up --build`. An absent or invalid worker ID
causes the worker to fail closed at startup; it does not prevent starting the
default API/PostgreSQL profile. The worker does not perform
re-execution. The sandboxed replay runner/UI/offline demo do not
yet exist as supported Compose services. Do not expose this setup to the
internet or claim recorded re-execution isolation until ANVIL-037/073 are
completed. Stop with `docker compose down` to retain database volume; use
`docker compose down --volumes` only when intentionally destroying data.
