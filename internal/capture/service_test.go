package capture

import (
	"encoding/base64"
	"errors"
	"testing"
	"time"

	"github.com/lewis-mcgillion/anvil/internal/events"
)

func captureScopes() map[string]bool { return map[string]bool{CaptureWrite: true, RunsRead: true} }

func TestCaptureEnforcesScopeAndBoundsArtifacts(t *testing.T) {
	s := NewService()
	if _, err := s.CreateRun("project", "", CreateRunRequest{AgentName: "agent", Adapter: "adk", AdapterVersion: "1"}, nil); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("missing token error = %v", err)
	}
	run, err := s.CreateRun("project", "create-1", CreateRunRequest{AgentName: "agent", Adapter: "adk", AdapterVersion: "1"}, captureScopes())
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.UploadArtifact(run.ID, "", ArtifactRequest{Content: base64.StdEncoding.EncodeToString(make([]byte, 8<<20+1))}, captureScopes())
	if !errors.Is(err, ErrTooLarge) {
		t.Fatalf("oversized artifact error = %v", err)
	}
}

func TestCaptureBatchIsIdempotentAndSealIsAsynchronous(t *testing.T) {
	s := NewService()
	run, err := s.CreateRun("project", "create-1", CreateRunRequest{AgentName: "agent", Adapter: "adk", AdapterVersion: "1", StartedAt: time.Now()}, captureScopes())
	if err != nil {
		t.Fatal(err)
	}
	event := events.Event{SchemaVersion: events.CurrentSchemaVersion, EventID: "event-1", RunID: run.ID, StreamID: "stream-1", Sequence: 0, Type: "run.completed", OccurredAt: time.Now(), Payload: []byte(`{"ok":true}`)}
	first, err := s.Ingest(run.ID, "batch-1", EventBatch{Events: []events.Event{event}}, captureScopes())
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.Ingest(run.ID, "", EventBatch{Events: []events.Event{event}}, captureScopes())
	if err != nil {
		t.Fatal(err)
	}
	if first.Accepted != 1 || second.Duplicates != 1 {
		t.Fatalf("unexpected results: %+v %+v", first, second)
	}
	job, err := s.Seal(run.ID, "seal-1", false, captureScopes())
	if err != nil {
		t.Fatal(err)
	}
	if job.ID == "" || job.URL != "/v1/jobs/"+job.ID {
		t.Fatalf("invalid job: %+v", job)
	}
}
