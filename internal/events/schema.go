// Package events validates Anvil's frozen, versioned event and recording
// JSON Schemas (schemas/v1). It is the Go implementation of the
// cross-language contract: the fixtures in fixtures/schema/v1 are validated
// by Go, Python and TypeScript and all three must agree.
//
// Validation is pure and in-memory; there is nothing to cancel and no
// resource to clean up. Compiled schemas are immutable and safe for
// concurrent use.
package events

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// SchemaName identifies one frozen v1 schema document.
type SchemaName string

const (
	SchemaEvent      SchemaName = "event"
	SchemaRun        SchemaName = "run"
	SchemaStream     SchemaName = "stream"
	SchemaArtifact   SchemaName = "artifact"
	SchemaSeal       SchemaName = "seal"
	SchemaCaptureGap SchemaName = "capture-gap"
	SchemaManifest   SchemaName = "manifest"
)

// allSchemas lists every frozen document validated by this package.
var allSchemas = []SchemaName{
	SchemaEvent, SchemaRun, SchemaStream, SchemaArtifact,
	SchemaSeal, SchemaCaptureGap, SchemaManifest,
}

// ValidationError reports that a document failed schema validation. It is
// returned explicitly; validators never silently accept or coerce input.
type ValidationError struct {
	Schema  SchemaName
	Details string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("anvil schema %s v1: validation failed: %s", e.Schema, e.Details)
}

var (
	compileOnce sync.Once
	compileErr  error
	compiled    = map[SchemaName]*jsonschema.Schema{}
)

// schemaDir returns the absolute path of the frozen schemas/v1 directory,
// resolved relative to this source file so tests and binaries work from any
// working directory.
func schemaDir() string {
	_, thisFile, _, _ := runtime.Caller(0)
	// internal/events/schema.go -> repo root is two directories up.
	root := filepath.Dir(filepath.Dir(filepath.Dir(thisFile)))
	return filepath.Join(root, "schemas", "v1")
}

func compile(name SchemaName) (*jsonschema.Schema, error) {
	compileOnce.Do(func() {
		compileErr = loadAll()
	})
	if compileErr != nil {
		return nil, compileErr
	}
	s, ok := compiled[name]
	if !ok {
		return nil, fmt.Errorf("anvil schema: unknown schema %q", string(name))
	}
	return s, nil
}

func loadAll() error {
	c := jsonschema.NewCompiler()
	c.DefaultDraft(jsonschema.Draft2020)
	c.AssertFormat()

	dir := schemaDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read schema dir: %w", err)
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".schema.json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, entry.Name()))
		if err != nil {
			return fmt.Errorf("read schema %s: %w", entry.Name(), err)
		}
		doc, err := jsonschema.UnmarshalJSON(strings.NewReader(string(data)))
		if err != nil {
			return fmt.Errorf("parse schema %s: %w", entry.Name(), err)
		}
		// Register under the absolute $id URL, since each schema declares
		// "$id": "https://anvil.dev/schemas/v1/<name>.json" and relative
		// $refs ("defs.json#/...") resolve against it.
		resource := "https://anvil.dev/schemas/v1/" + strings.TrimSuffix(entry.Name(), ".schema.json") + ".json"
		if err := c.AddResource(resource, doc); err != nil {
			return fmt.Errorf("add schema %s: %w", entry.Name(), err)
		}
	}
	for _, n := range allSchemas {
		s, err := c.Compile("https://anvil.dev/schemas/v1/" + string(n) + ".json")
		if err != nil {
			return fmt.Errorf("compile schema %s: %w", n, err)
		}
		compiled[n] = s
	}
	return nil
}

// Validate unmarshals data as JSON and checks it against the named v1
// schema. It returns nil for valid documents and a *ValidationError
// otherwise. Malformed JSON is reported as a *ValidationError too.
func Validate(name SchemaName, data []byte) error {
	schema, err := compile(name)
	if err != nil {
		return err
	}
	doc, err := jsonschema.UnmarshalJSON(strings.NewReader(string(data)))
	if err != nil {
		return &ValidationError{Schema: name, Details: "invalid JSON: " + err.Error()}
	}
	if err := schema.Validate(doc); err != nil {
		var ve *jsonschema.ValidationError
		if errors.As(err, &ve) {
			return &ValidationError{Schema: name, Details: summarize(ve)}
		}
		return &ValidationError{Schema: name, Details: err.Error()}
	}
	return nil
}

// ValidateEvent is a convenience wrapper for the event schema.
func ValidateEvent(data []byte) error { return Validate(SchemaEvent, data) }

// ValidateManifest is a convenience wrapper for the manifest schema.
func ValidateManifest(data []byte) error { return Validate(SchemaManifest, data) }

// summarize flattens nested validation causes into a stable, bounded
// single-line message.
func summarize(ve *jsonschema.ValidationError) string {
	var parts []string
	var walk func(e *jsonschema.ValidationError)
	walk = func(e *jsonschema.ValidationError) {
		if len(e.Causes) == 0 {
			loc := "/" + strings.Join(e.InstanceLocation, "/")
			parts = append(parts, fmt.Sprintf("%v at %s", e.ErrorKind, loc))
			return
		}
		for _, cause := range e.Causes {
			walk(cause)
		}
	}
	walk(ve)
	if len(parts) == 0 {
		return ve.Error()
	}
	if len(parts) > 4 {
		parts = parts[:4]
	}
	return strings.Join(parts, "; ")
}
