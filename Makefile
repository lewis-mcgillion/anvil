# Anvil contract tooling. Pinned tool versions are restored on demand.

BUF_VERSION := 1.47.2
OAPI_CODEGEN_VERSION := v2.4.1
PROTOC_GEN_GO_VERSION := v1.36.5

BIN := $(CURDIR)/bin
export PATH := $(BIN):$(PATH)

.PHONY: tools generate lint test verify

tools: $(BIN)/buf $(BIN)/protoc-gen-go $(BIN)/oapi-codegen

$(BIN)/buf:
	mkdir -p $(BIN)
	curl -sSfL -o $@ "https://github.com/bufbuild/buf/releases/download/v$(BUF_VERSION)/buf-$$(uname -s)-$$(uname -m)"
	chmod +x $@

$(BIN)/protoc-gen-go:
	GOBIN=$(BIN) go install google.golang.org/protobuf/cmd/protoc-gen-go@$(PROTOC_GEN_GO_VERSION)

$(BIN)/oapi-codegen:
	GOBIN=$(BIN) go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@$(OAPI_CODEGEN_VERSION)

generate: tools
	buf lint
	buf generate
	$(BIN)/oapi-codegen -generate types,client -package v1 -o api/gen/v1/client.gen.go api/openapi.yaml
	cd web && npm ci --no-audit --no-fund && npm run generate

lint: tools
	buf lint

test:
	cd internal/contracts && go test ./...
	cd runner/gen && go build ./...
	cd api/gen && go build ./...
	cd web && npx tsc --noEmit

verify: generate test
	@echo "All contract checks passed."
