# Local-only Compose foundation

Set `ANVIL_POSTGRES_PASSWORD` to a URL-safe strong value and
`ANVIL_CURSOR_SECRET` to a distinct random value of at least 16 characters.
From `deploy/compose`, run `docker compose up --build`. API access is bound to
`127.0.0.1:8080`, and PostgreSQL is not published to the host.

This is **not yet** a complete Anvil installation. On a new, empty database,
start PostgreSQL with `docker compose up -d postgres`, then apply the SQL
migrations explicitly, stopping on any failure:

```sh
for migration in 0001_core 0002_jobs 0002_authorization 0002_administration \
  0003_run_metadata 0004_seal_jobs 0005_agent_versions 0006_archive_jobs \
  0007_comparisons 0008_privacy_deletion; do
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U anvil -d anvil \
    -f "/anvil-migrations/${migration}.sql" || exit 1
done
docker compose up -d --build api
```

Do not rerun these commands against an existing database without inspecting
`schema_migrations` and confirming the required upgrade path. The mounted
migration directory is read-only. The worker requires an explicitly
configured project ID (`ANVIL_WORKER_PROJECT_ID`) and is opt-in via
`docker compose --profile worker up --build`. An absent or invalid worker ID
causes the worker to fail closed at startup; it does not prevent starting the
default API/PostgreSQL profile. The worker does not perform
re-execution. The sandboxed replay runner/UI/offline demo do not
yet exist as supported Compose services. Do not expose this setup to the
internet or claim recorded re-execution isolation until ANVIL-037/073 are
completed. Stop with `docker compose down` to retain database volume; use
`docker compose down --volumes` only when intentionally destroying data.
