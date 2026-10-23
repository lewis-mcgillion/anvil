// Package contracts holds contract tests pinning the public REST and
// runner protocol surfaces. The OpenAPI spec and protobuf definitions are
// the source of truth; these tests fail when the contract drifts from the
// documented semantics in docs/architecture.md and docs/api-contract.md.
package contracts

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"github.com/getkin/kin-openapi/openapi3"
)

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func loadSpec(t *testing.T) *openapi3.T {
	t.Helper()
	loader := openapi3.NewLoader()
	doc, err := loader.LoadFromFile(filepath.Join(repoRoot(t), "api", "openapi.yaml"))
	if err != nil {
		t.Fatalf("load spec: %v", err)
	}
	if err := doc.Validate(context.Background()); err != nil {
		t.Fatalf("spec invalid: %v", err)
	}
	return doc
}

// specPaths mirrors the Public API section of docs/architecture.md. Keep
// it in sync when endpoints are added or removed.
func specPaths() map[string][]string {
	return map[string][]string{
		"/projects":                                {"post", "get"},
		"/projects/{project_id}":                   {"patch"},
		"/projects/{project_id}/keys":              {"post"},
		"/projects/{project_id}/keys/{key_id}":     {"delete"},
		"/projects/{project_id}/runs":              {"post"},
		"/runs":                                    {"get"},
		"/runs/{run_id}":                           {"get"},
		"/runs/{run_id}/events:batch":              {"post"},
		"/runs/{run_id}/events":                    {"get"},
		"/runs/{run_id}/artifacts":                 {"post"},
		"/runs/{run_id}:seal":                      {"post"},
		"/runs/{run_id}/watch":                     {"get"},
		"/recordings/{recording_id}":               {"get"},
		"/recordings/{recording_id}/projection":    {"get"},
		"/recordings/{recording_id}/exports":       {"post"},
		"/projects/{project_id}/imports":           {"post"},
		"/projects/{project_id}/agent-versions":    {"post"},
		"/reexecutions":                            {"post"},
		"/reexecutions/{reexecution_id}":           {"get"},
		"/jobs/{job_id}":                           {"get"},
		"/jobs/{job_id}:cancel":                    {"post"},
		"/comparisons":                             {"post"},
		"/comparisons/{comparison_id}":             {"get"},
		"/comparisons/{comparison_id}/divergences": {"get"},
		"/projects/{project_id}/suites":            {"post"},
		"/suites/{suite_id}/versions":              {"post"},
		"/suite-runs":                              {"post"},
		"/suite-runs/{suite_run_id}":               {"get"},
		"/suite-runs/{suite_run_id}/report":        {"get"},
		"/projects/{project_id}/deletions":         {"post"},
		"/deletions/{deletion_id}":                 {"get"},
		"/health/live":                             {"get"},
		"/health/ready":                            {"get"},
		"/metrics":                                 {"get"},
	}
}

func TestSpecCoversArchitecturePaths(t *testing.T) {
	doc := loadSpec(t)
	for path, methods := range specPaths() {
		item := doc.Paths.Map()[path]
		if item == nil {
			t.Errorf("missing path %s", path)
			continue
		}
		for _, m := range methods {
			if item.GetOperation(strings.ToUpper(m)) == nil {
				t.Errorf("path %s missing %s operation", path, strings.ToUpper(m))
			}
		}
	}
	if got, want := len(doc.Paths.Map()), len(specPaths()); got != want {
		t.Errorf("spec has %d paths, contract test expects %d: update both together", got, want)
	}
}

func TestSpecNoPlaceholderPaths(t *testing.T) {
	doc := loadSpec(t)
	for path := range doc.Paths.Map() {
		if strings.Contains(path, "TODO") || strings.Contains(path, "placeholder") {
			t.Errorf("placeholder path leaked into spec: %s", path)
		}
	}
}

// TestEveryOperationHasOperationID ensures generated clients get stable,
// compilable method names for every operation.
func TestEveryOperationHasOperationID(t *testing.T) {
	doc := loadSpec(t)
	seen := map[string]string{}
	for path, item := range doc.Paths.Map() {
		for method, op := range item.Operations() {
			if op.OperationID == "" {
				t.Errorf("%s %s has no operationId", method, path)
				continue
			}
			if prev, dup := seen[op.OperationID]; dup {
				t.Errorf("operationId %q used by both %s and %s %s", op.OperationID, prev, method, path)
			}
			seen[op.OperationID] = method + " " + path
		}
	}
}

func problemJSONResponse(t *testing.T, op *openapi3.Operation, code string) bool {
	t.Helper()
	resp := op.Responses.Map()[code]
	if resp == nil || resp.Value == nil {
		return false
	}
	mt := resp.Value.Content.Get("application/problem+json")
	if mt == nil || mt.Schema == nil || mt.Schema.Ref == "" {
		return false
	}
	return strings.HasSuffix(mt.Schema.Ref, "/Problem")
}

// TestErrorContract pins: problem+json on 400/401/403/404/409 for every
// operation, 429 with Retry-After on every non-health operation, and 413
// on payload-accepting endpoints.
func TestErrorContract(t *testing.T) {
	doc := loadSpec(t)
	for path, item := range doc.Paths.Map() {
		for method, op := range item.Operations() {
			where := method + " " + path
			for _, code := range []string{"400", "401", "403", "404", "409"} {
				if op.Responses.Map()[code] == nil {
					continue // not every operation returns every code
				}
				if !problemJSONResponse(t, op, code) {
					t.Errorf("%s: response %s must be application/problem+json with Problem schema", where, code)
				}
			}
			if strings.HasPrefix(path, "/health/") {
				continue
			}
			throttle := op.Responses.Map()["429"]
			if throttle == nil {
				t.Errorf("%s: missing 429 throttling response", where)
				continue
			}
			ref := throttle.Ref
			if ref == "" && throttle.Value != nil {
				if h := throttle.Value.Headers["Retry-After"]; h == nil {
					t.Errorf("%s: 429 response lacks Retry-After header", where)
				}
			} else if !strings.Contains(ref, "Throttled") {
				t.Errorf("%s: 429 response must reference the shared Throttled response (Retry-After)", where)
			}
		}
	}
	for _, ep := range []struct{ path, method string }{
		{"/runs/{run_id}/events:batch", "post"},
		{"/runs/{run_id}/artifacts", "post"},
		{"/projects/{project_id}/imports", "post"},
	} {
		op := doc.Paths.Map()[ep.path].GetOperation(strings.ToUpper(ep.method))
		if op.Responses.Map()["413"] == nil {
			t.Errorf("%s %s: bounded payload endpoint must document 413", ep.method, ep.path)
		}
	}
}

// TestIdempotencyKeysOnCreations pins the idempotency-key requirement on
// all resource-creating endpoints.
func TestIdempotencyKeysOnCreations(t *testing.T) {
	doc := loadSpec(t)
	creating := []struct{ path, method string }{
		{"/projects", "post"},
		{"/projects/{project_id}/keys", "post"},
		{"/projects/{project_id}/runs", "post"},
		{"/runs/{run_id}/events:batch", "post"},
		{"/runs/{run_id}/artifacts", "post"},
		{"/runs/{run_id}:seal", "post"},
		{"/recordings/{recording_id}/exports", "post"},
		{"/projects/{project_id}/imports", "post"},
		{"/projects/{project_id}/agent-versions", "post"},
		{"/reexecutions", "post"},
		{"/comparisons", "post"},
		{"/projects/{project_id}/suites", "post"},
		{"/suites/{suite_id}/versions", "post"},
		{"/suite-runs", "post"},
		{"/projects/{project_id}/deletions", "post"},
	}
	for _, ep := range creating {
		op := doc.Paths.Map()[ep.path].GetOperation(strings.ToUpper(ep.method))
		found := false
		for _, pref := range op.Parameters {
			if pref.Value != nil && pref.Value.Name == "Idempotency-Key" && pref.Value.In == "header" && pref.Value.Required {
				found = true
			}
		}
		if !found {
			t.Errorf("%s %s: creating endpoint must require the Idempotency-Key header", ep.method, ep.path)
		}
	}
}

// TestAsyncJobs pins 202 semantics for asynchronous endpoints.
func TestAsyncJobs(t *testing.T) {
	doc := loadSpec(t)
	async := []struct{ path, method string }{
		{"/runs/{run_id}:seal", "post"},
		{"/recordings/{recording_id}/exports", "post"},
		{"/projects/{project_id}/imports", "post"},
		{"/reexecutions", "post"},
		{"/comparisons", "post"},
		{"/suite-runs", "post"},
		{"/projects/{project_id}/deletions", "post"},
	}
	for _, ep := range async {
		op := doc.Paths.Map()[ep.path].GetOperation(strings.ToUpper(ep.method))
		if op.Responses.Map()["202"] == nil {
			t.Errorf("%s %s: asynchronous endpoint must return 202", ep.method, ep.path)
		}
	}
}

// TestPaginationContract pins cursor pagination on every list endpoint.
func TestPaginationContract(t *testing.T) {
	doc := loadSpec(t)
	lists := []string{
		"/projects",
		"/runs",
		"/runs/{run_id}/events",
		"/comparisons/{comparison_id}/divergences",
	}
	for _, path := range lists {
		op := doc.Paths.Map()[path].GetOperation("GET")
		var hasCursor, hasLimit bool
		for _, pref := range op.Parameters {
			if pref.Value == nil {
				continue
			}
			switch pref.Value.Name {
			case "cursor":
				hasCursor = true
			case "limit":
				hasLimit = true
				if pref.Value.Schema.Value.Max == nil || *pref.Value.Schema.Value.Max != 1000 {
					t.Errorf("%s: limit must cap at 1000", path)
				}
			}
		}
		if !hasCursor || !hasLimit {
			t.Errorf("%s: list endpoint must accept cursor and limit", path)
		}
		resp := op.Responses.Map()["200"].Value
		mt := resp.Content.Get("application/json")
		if mt == nil {
			t.Errorf("%s: list endpoint must return JSON", path)
		}
	}
}
