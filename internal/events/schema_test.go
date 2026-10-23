package events

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// fixtureRoot returns the shared cross-language fixture corpus. The Python
// and TypeScript suites iterate exactly the same files; all three validators
// must agree on every case.
func fixtureRoot() string {
	_, thisFile, _, _ := runtime.Caller(0)
	root := filepath.Dir(filepath.Dir(filepath.Dir(thisFile)))
	return filepath.Join(root, "fixtures", "schema", "v1")
}

func fixtureCases(t *testing.T) []struct {
	schema SchemaName
	name   string
	path   string
	valid  bool
} {
	t.Helper()
	base := fixtureRoot()
	entries, err := os.ReadDir(base)
	if err != nil {
		t.Fatalf("read fixture root: %v", err)
	}
	var cases []struct {
		schema SchemaName
		name   string
		path   string
		valid  bool
	}
	for _, dir := range entries {
		if !dir.IsDir() {
			continue
		}
		schema := SchemaName(dir.Name())
		files, err := os.ReadDir(filepath.Join(base, dir.Name()))
		if err != nil {
			t.Fatalf("read fixture dir %s: %v", dir.Name(), err)
		}
		for _, f := range files {
			if f.IsDir() || !strings.HasSuffix(f.Name(), ".json") {
				continue
			}
			var valid bool
			switch {
			case strings.HasSuffix(f.Name(), ".valid.json"):
				valid = true
			case strings.HasSuffix(f.Name(), ".invalid.json"):
				valid = false
			default:
				t.Fatalf("fixture %s must end in .valid.json or .invalid.json", f.Name())
			}
			cases = append(cases, struct {
				schema SchemaName
				name   string
				path   string
				valid  bool
			}{schema: schema, name: f.Name(), path: filepath.Join(base, dir.Name(), f.Name()), valid: valid})
		}
	}
	if len(cases) == 0 {
		t.Fatal("no schema fixtures found; the cross-language corpus must not be empty")
	}
	return cases
}

// TestFixtureCorpusAgreement is the cross-language contract test: every
// positive fixture validates, every negative fixture is rejected explicitly.
func TestFixtureCorpusAgreement(t *testing.T) {
	for _, tc := range fixtureCases(t) {
		t.Run(string(tc.schema)+"/"+tc.name, func(t *testing.T) {
			data, err := os.ReadFile(tc.path)
			if err != nil {
				t.Fatalf("read fixture: %v", err)
			}
			err = Validate(tc.schema, data)
			if tc.valid && err != nil {
				t.Fatalf("positive fixture rejected: %v", err)
			}
			if !tc.valid {
				if err == nil {
					t.Fatal("negative fixture accepted")
				}
				var ve *ValidationError
				if !errors.As(err, &ve) {
					t.Fatalf("negative fixture must fail with *ValidationError, got %T: %v", err, err)
				}
			}
		})
	}
}

func TestMalformedJSON(t *testing.T) {
	err := Validate(SchemaEvent, []byte(`{"schema_version": "1",`))
	var ve *ValidationError
	if !errors.As(err, &ve) {
		t.Fatalf("malformed JSON must fail with *ValidationError, got %v", err)
	}
	if !strings.Contains(ve.Details, "invalid JSON") {
		t.Fatalf("malformed JSON error should say so, got %q", ve.Details)
	}
}

func TestUnknownSchema(t *testing.T) {
	if err := Validate(SchemaName("bogus"), []byte(`{}`)); err == nil {
		t.Fatal("unknown schema name must fail explicitly")
	}
}

// TestSchemasCompile guards the frozen documents themselves: a syntax error
// or dangling $ref in schemas/v1 must fail loudly here, not in production.
func TestSchemasCompile(t *testing.T) {
	for _, name := range allSchemas {
		if _, err := compile(name); err != nil {
			t.Fatalf("schema %s: %v", name, err)
		}
	}
}

// TestEmbeddedSchemasMatchRepo guards against drift: the embedded copies
// under internal/events/schemas/v1 must be byte-identical to the
// authoritative frozen documents in schemas/v1.
func TestEmbeddedSchemasMatchRepo(t *testing.T) {
	_, thisFile, _, _ := runtime.Caller(0)
	repoRoot := filepath.Dir(filepath.Dir(filepath.Dir(thisFile)))
	entries, err := os.ReadDir(filepath.Join(repoRoot, "schemas", "v1"))
	if err != nil {
		t.Fatalf("read repo schemas: %v", err)
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".schema.json") {
			continue
		}
		want, err := os.ReadFile(filepath.Join(repoRoot, "schemas", "v1", entry.Name()))
		if err != nil {
			t.Fatalf("read repo schema %s: %v", entry.Name(), err)
		}
		got, err := frozenSchemas.ReadFile("schemas/v1/" + entry.Name())
		if err != nil {
			t.Fatalf("embedded schema %s missing: %v", entry.Name(), err)
		}
		if string(got) != string(want) {
			t.Fatalf("embedded schema %s differs from schemas/v1/%s; refresh internal/events/schemas/v1", entry.Name(), entry.Name())
		}
	}
}

func TestValidationErrorMessage(t *testing.T) {
	err := Validate(SchemaSeal, []byte(`{"schema_version":"1"}`))
	if err == nil {
		t.Fatal("incomplete seal request must be rejected")
	}
	if !strings.Contains(err.Error(), "anvil schema seal v1") {
		t.Fatalf("error must name the schema and version, got %q", err.Error())
	}
}
