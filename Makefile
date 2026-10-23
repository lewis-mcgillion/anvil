.PHONY: build format lint test containers

build:
	go build ./...
	python3 -m compileall -q sdk/python/anvil
	npm --prefix web run build

format:
	test -z "$$(gofmt -l $$(find . -name '*.go' -not -path './.git/*'))"
	python3 -m compileall -q sdk/python/anvil
	npm --prefix web run format

lint:
	go vet ./...
	python3 -m compileall -q sdk/python/anvil
	npm --prefix web run lint

test:
	go test ./...
	PYTHONPATH=sdk/python python3 -m unittest discover -s sdk/python/tests
	npm --prefix web test

containers:
	docker build --file deploy/containers/api.Dockerfile .
	docker build --file deploy/containers/sdk.Dockerfile sdk/python
