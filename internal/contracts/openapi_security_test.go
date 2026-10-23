package contracts

import (
	"strings"
	"testing"
)

// requiredScopes pins the authorization matrix: every operation and the
// minimum scope that must satisfy it. Capture keys must never read or
// replay; playback is read-only.
func requiredScopes() map[string]string {
	return map[string]string{
		"post /projects":                               "admin",
		"get /projects":                                "runs:read",
		"patch /projects/{project_id}":                 "admin",
		"post /projects/{project_id}/keys":             "admin",
		"delete /projects/{project_id}/keys/{key_id}":  "admin",
		"post /projects/{project_id}/runs":             "capture:write",
		"post /runs/{run_id}/events:batch":             "capture:write",
		"post /runs/{run_id}/artifacts":                "capture:write",
		"post /runs/{run_id}:seal":                     "capture:write",
		"get /runs":                                    "runs:read",
		"get /runs/{run_id}":                           "runs:read",
		"get /runs/{run_id}/events":                    "runs:read",
		"get /runs/{run_id}/watch":                     "runs:read",
		"get /recordings/{recording_id}":               "runs:read",
		"get /recordings/{recording_id}/projection":    "runs:read",
		"post /recordings/{recording_id}/exports":      "runs:read",
		"post /projects/{project_id}/imports":          "replay:write",
		"post /projects/{project_id}/agent-versions":   "replay:write",
		"post /reexecutions":                           "replay:write",
		"get /reexecutions/{reexecution_id}":           "runs:read",
		"get /jobs/{job_id}":                           "runs:read",
		"post /jobs/{job_id}:cancel":                   "replay:write",
		"post /comparisons":                            "replay:write",
		"get /comparisons/{comparison_id}":             "runs:read",
		"get /comparisons/{comparison_id}/divergences": "runs:read",
		"post /projects/{project_id}/suites":           "suites:write",
		"post /suites/{suite_id}/versions":             "suites:write",
		"post /suite-runs":                             "suites:write",
		"get /suite-runs/{suite_run_id}":               "suites:read",
		"get /suite-runs/{suite_run_id}/report":        "suites:read",
		"post /projects/{project_id}/deletions":        "admin",
		"get /deletions/{deletion_id}":                 "admin",
		"get /metrics":                                 "metrics:read",
	}
}

func TestAuthScopeMatrix(t *testing.T) {
	doc := loadSpec(t)
	for key, wantScope := range requiredScopes() {
		parts := strings.SplitN(key, " ", 2)
		op := doc.Paths.Map()[parts[1]].GetOperation(strings.ToUpper(parts[0]))
		if op == nil {
			t.Errorf("%s: operation missing from spec", key)
			continue
		}
		if op.Security == nil || len(*op.Security) == 0 {
			t.Errorf("%s: must declare security requirement", key)
			continue
		}
		scopes := (*op.Security)[0]["bearerAuth"]
		if len(scopes) != 1 || scopes[0] != wantScope {
			t.Errorf("%s: scope = %v, want [%s]", key, scopes, wantScope)
		}
	}
}

func TestHealthEndpointsAreUnauthenticated(t *testing.T) {
	doc := loadSpec(t)
	for _, path := range []string{"/health/live", "/health/ready"} {
		op := doc.Paths.Map()[path].GetOperation("GET")
		if op.Security == nil {
			t.Errorf("%s: must explicitly override security with an empty requirement", path)
			continue
		}
		if len(*op.Security) != 0 {
			t.Errorf("%s: health probes must not require scopes", path)
		}
	}
}

// TestCaptureScopeCannotReadOrReplay encodes the role-matrix rule from
// docs/architecture.md: capture keys are denied read and replay
// permissions, at the contract level.
func TestCaptureScopeCannotReadOrReplay(t *testing.T) {
	doc := loadSpec(t)
	for path, item := range doc.Paths.Map() {
		for method, op := range item.Operations() {
			if op.Security == nil || len(*op.Security) == 0 {
				continue
			}
			scopes := (*op.Security)[0]["bearerAuth"]
			for _, s := range scopes {
				if s != "capture:write" {
					continue
				}
				if method != "POST" {
					t.Errorf("%s %s: capture:write must only appear on capture submissions", method, path)
				}
				capturePath := strings.HasPrefix(path, "/projects/{project_id}/runs") ||
					strings.HasPrefix(path, "/runs/{run_id}/") ||
					strings.HasPrefix(path, "/runs/{run_id}:") ||
					strings.HasPrefix(path, "/projects/{project_id}/keys")
				if !capturePath {
					t.Errorf("%s %s: capture:write used outside the capture surface", method, path)
				}
			}
		}
	}
}

// TestEnumSnapshots pins public enums so silent contract drift fails CI.
func TestEnumSnapshots(t *testing.T) {
	doc := loadSpec(t)
	want := map[string][]string{
		"EventType": {
			"run.started", "run.completed", "run.failed", "message.created",
			"model.requested", "model.chunk", "model.completed", "model.failed",
			"tool.requested", "tool.completed", "tool.failed",
			"state.checkpoint", "capture.gap", "fault.applied",
		},
		"CaptureState":          {"open", "sealing", "sealed_complete", "sealed_incomplete"},
		"ExecutionState":        {"running", "succeeded", "failed", "cancelled", "unknown"},
		"ModelMode":             {"recorded", "live"},
		"FixtureMatchingPolicy": {"strict"},
		"JobStatus":             {"queued", "running", "cancelling", "cancelled", "succeeded", "failed"},
		"DeterministicStatus":   {"pass", "fail", "inconclusive"},
		"JudgeStatus":           {"pass", "fail", "inconclusive", "not_run"},
		"InfrastructureStatus":  {"ok", "error", "cancelled", "timed_out"},
		"AlignmentConfidence":   {"exact", "partial", "ambiguous"},
		"ApiScope": {
			"capture:write", "runs:read", "replay:write", "suites:write",
			"suites:read", "admin", "metrics:read",
		},
	}
	for name, wantValues := range want {
		ref := doc.Components.Schemas[name]
		if ref == nil || ref.Value == nil {
			t.Errorf("schema %s missing", name)
			continue
		}
		var got []string
		for _, v := range ref.Value.Enum {
			if s, ok := v.(string); ok {
				got = append(got, s)
			}
		}
		if strings.Join(got, ",") != strings.Join(wantValues, ",") {
			t.Errorf("enum %s changed: got %v want %v (update docs/compatibility.md if intentional)", name, got, wantValues)
		}
	}
}
