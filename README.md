# Anvil

Anvil is an open-source reliability, replay and regression-testing platform for AI agents.

**Status:** Python capture foundation under development. Production-ready execution and security guarantees are not implemented yet.

The [product initiative](https://github.com/lewis-mcgillion/anvil/issues/1) contains 14 epics and 84 implementation tasks. See [architecture and technical plan](docs/architecture.md) for the APIs, data contracts, execution models, security controls, testing strategy and rollout milestones, plus the [architecture decision records](docs/adr/README.md) and [threat model](docs/threat-model.md) that govern those contracts.

Anvil's primary workflow is to capture a failed Google ADK run, inspect it, safely reproduce it with mocked tool responses and turn the failure into a version-controlled regression test. The runtime uses Python (FastAPI, Pydantic, SQLAlchemy, asyncpg), PostgreSQL and React with TypeScript.

Licensed under Apache-2.0.

## Development

The Python service requires Python 3.12, PostgreSQL 18 and Node.js 20+ for the dashboard.
Install `server/pyproject.toml` and run `python -m pytest server/tests` to test the new API foundation.
The Python implementation is validated with `make test` and `make build`. Go is not required.
The dependency lockfiles are `sdk/python/requirements.lock` and
`web/package-lock.json`; dependency changes must update the corresponding lockfile.
Apply SQL files in `migrations/` in the order documented in
`docs/compatibility.md` before starting the API. Set `ANVIL_DATABASE_URL`
to the asyncpg connection URL and `ANVIL_CURSOR_SECRET` to a stable,
random secret of at least 16 characters for cursor continuity across restarts.
For asynchronous sealing, start a Python worker per project with
`ANVIL_WORKER_PROJECT_ID` set to that project's UUID and run
`python -m anvil_server.worker` from the installed `server` package.
Apply all migrations through `0005_agent_versions` before using the
agent-version registration endpoint. Registering an image does not run it.

The API container runs Python and does not require Go. Capture, PostgreSQL event
storage, sealing, immutable recording retrieval, and read-only projection have
Python implementations. Project administration and scoped key lifecycle have
initial endpoints. These are not yet a complete implementation of the frozen
`api/openapi.yaml` contract: resource-only endpoints, asynchronous export/import,
deletion workers, durable retention enforcement, complete idempotency,
re-execution, comparisons, suites and the isolated runner still require work.
The Python service must not be treated as a production replay engine. Replay
must never invoke production tools, and must fail closed if isolation or
fixtures cannot be guaranteed.
