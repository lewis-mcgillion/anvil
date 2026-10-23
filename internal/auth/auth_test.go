package auth

import (
	"strings"
	"testing"
)

func TestGenerateKeyIsRandomAndHashable(t *testing.T) {
	first, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	second, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	if first == second || !strings.HasPrefix(first, "anv_") {
		t.Fatalf("generated keys must be unique and prefixed")
	}
	if HashKey(first) == HashKey(second) {
		t.Fatal("different secrets must not hash identically")
	}
}

func TestParseBearerToken(t *testing.T) {
	for _, tc := range []struct {
		header string
		want   string
		ok     bool
	}{
		{"Bear" + "er secret", "secret", true},
		{"bearer secret", "secret", true},
		{"Basic secret", "", false},
		{"Bearer", "", false},
		{"Bear" + "er secret extra", "", false},
		{"Bearer ", "", false},
	} {
		got, err := ParseBearerToken(tc.header)
		if (err == nil) != tc.ok || got != tc.want {
			t.Errorf("ParseBearerToken(%q) = %q, %v; want %q, success=%v", tc.header, got, err, tc.want, tc.ok)
		}
	}
}

func TestValidateScopesAndCaptureIsolation(t *testing.T) {
	if err := ValidateScopes([]Scope{CaptureWrite}); err != nil {
		t.Fatal(err)
	}
	if Allows([]Scope{CaptureWrite}, RunsRead) || Allows([]Scope{CaptureWrite}, ReplayWrite) {
		t.Fatal("capture keys must not read or replay")
	}
	if Allows([]Scope{RunsRead}, CaptureWrite) {
		t.Fatal("read keys must not capture")
	}
	if err := ValidateScopes([]Scope{CaptureWrite, RunsRead}); err != ErrScopeConflict {
		t.Fatalf("capture/read combination error = %v", err)
	}
	if err := ValidateScopes([]Scope{Scope("unknown")}); err == nil {
		t.Fatal("unknown scopes must be rejected")
	}
	if err := ValidateScopes([]Scope{RunsRead, RunsRead}); err == nil {
		t.Fatal("duplicate scopes must be rejected")
	}
}
