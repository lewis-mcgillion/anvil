.PHONY: generate test lint build

generate:
	cd web && npm ci --no-audit --no-fund && npm run generate

test:
	python -m pytest -q server/tests sdk/python/tests tests
	cd web/schemas && npm ci --no-audit --no-fund && npm test

lint:
	python -m compileall -q server/anvil_server sdk/python/anvil sdk/python/anvil_schema
	cd web && npm run build

build:
	python -m pip wheel --no-deps server -w /tmp/anvil-dist
	cd web && npm run build
