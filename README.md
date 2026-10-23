# Anvil

Anvil is an open-source reliability, replay and regression-testing platform for AI agents.

**Status:** Planning. No production-ready execution or security guarantees are implemented yet.

The [product initiative](https://github.com/lewis-mcgillion/anvil/issues/1) contains 14 epics and 84 implementation tasks. See [architecture and technical plan](docs/architecture.md) for the APIs, data contracts, execution models, security controls, testing strategy and rollout milestones.

Anvil's primary workflow is to capture a failed Google ADK run, inspect it, safely reproduce it with mocked tool responses and turn the failure into a version-controlled regression test. Initial development is local-first and uses Go, PostgreSQL, Python and React with TypeScript.

Licensed under Apache-2.0.

## Development

The foundation checks require Go 1.23+, Python 3.11+ and Node.js 20+. Run
`make format`, `make lint`, `make test` and `make build` from a clean checkout.
The dependency lockfiles are `sdk/python/requirements.lock` and
`web/package-lock.json`; dependency changes must update the corresponding lockfile.

The API process handles `SIGINT` and `SIGTERM`, stops accepting new requests,
and gives in-flight requests up to ten seconds to finish. The container builds
use no credentials or network at runtime. Replay and playback components are not
implemented by this foundation and must preserve the architecture guarantee that
production tools are never executed.
