package sealing

import (
	"errors"
	"testing"
)

func completeRun() Run {
	r := Run{ID: "run-1", ProjectID: "project-1"}
	_ = r.AddStream(Stream{ID: "stream-1", Terminal: true})
	_ = r.AddEvent(Event{ID: "start", StreamID: "stream-1", Sequence: 0, Type: "run.started", ReplaySafe: true})
	_ = r.AddEvent(Event{ID: "finish", StreamID: "stream-1", Sequence: 1, Type: "run.completed", ParentEventIDs: []string{"start"}, ReplaySafe: true})
	return r
}

func TestSealOutOfOrderEventsAndCanonicalManifest(t *testing.T) {
	r := Run{ID: "run-1", ProjectID: "project-1"}
	if err := r.AddStream(Stream{ID: "stream-1", Terminal: true}); err != nil {
		t.Fatal(err)
	}
	for _, event := range []Event{
		{ID: "finish", StreamID: "stream-1", Sequence: 1, Type: "run.completed", ParentEventIDs: []string{"start"}, ReplaySafe: true},
		{ID: "start", StreamID: "stream-1", Sequence: 0, Type: "run.started", ReplaySafe: true},
	} {
		if err := r.AddEvent(event); err != nil {
			t.Fatal(err)
		}
	}
	result, err := r.Seal(SealRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if result.Manifest.CaptureCompleteness != SealedComplete ||
		result.Manifest.ExecutionOutcome != OutcomeSucceeded ||
		result.Manifest.ReplayEligibility != ReplayEligible {
		t.Fatalf("unexpected manifest: %+v", result.Manifest)
	}
	if result.Manifest.PayloadSHA256 == "" {
		t.Fatal("manifest must contain a payload hash")
	}
}

func TestSealRejectsIncompleteUnlessAcknowledged(t *testing.T) {
	r := completeRun()
	_ = r.AddGap(Gap{StreamID: "stream-1", From: 2, To: 2, Reason: "exporter timeout"})
	if _, err := r.Seal(SealRequest{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("expected incomplete rejection, got %v", err)
	}
	result, err := r.Seal(SealRequest{AcknowledgeIncomplete: true})
	if err != nil {
		t.Fatal(err)
	}
	if result.Manifest.CaptureCompleteness != SealedIncomplete ||
		result.Manifest.ReplayEligibility != ReplayIneligible {
		t.Fatalf("unexpected incomplete manifest: %+v", result.Manifest)
	}
}

func TestSealDetectsCausalityAndInvocationIssues(t *testing.T) {
	r := Run{ID: "run-1", ProjectID: "project-1"}
	_ = r.AddStream(Stream{ID: "stream-1", Terminal: true})
	_ = r.AddEvent(Event{ID: "request", StreamID: "stream-1", Sequence: 0, Type: "tool.requested", InvocationID: "call-1", ReplaySafe: true})
	_ = r.AddEvent(Event{ID: "finish", StreamID: "stream-1", Sequence: 1, Type: "run.completed", ParentEventIDs: []string{"missing"}, ReplaySafe: true})
	if _, err := r.Seal(SealRequest{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("expected incomplete rejection, got %v", err)
	}
	result, err := r.Seal(SealRequest{AcknowledgeIncomplete: true})
	if err != nil {
		t.Fatal(err)
	}
	if result.Manifest.ReplayEligibility != ReplayIneligible {
		t.Fatal("causality and unmatched invocation must disable replay")
	}
}

func TestSealedRunIsImmutable(t *testing.T) {
	r := completeRun()
	result, err := r.Seal(SealRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if err := r.AddEvent(Event{ID: "late", StreamID: "stream-1", Sequence: 2, Type: "message.created", ReplaySafe: true}); !errors.Is(err, ErrSealed) {
		t.Fatalf("late event must be rejected, got %v", err)
	}
	if _, err := r.Seal(SealRequest{}); !errors.Is(err, ErrSealed) {
		t.Fatalf("second seal must be rejected, got %v", err)
	}
	if result.Manifest.CaptureCompleteness != SealedComplete {
		t.Fatal("manifest changed unexpectedly")
	}
}

func TestSealRejectsConflictingSequenceEvidence(t *testing.T) {
	r := Run{ID: "run-1", ProjectID: "project-1"}
	_ = r.AddStream(Stream{ID: "stream-1", Terminal: true})
	_ = r.AddEvent(Event{ID: "one", StreamID: "stream-1", Sequence: 0, Type: "run.started", ReplaySafe: true})
	_ = r.AddEvent(Event{ID: "two", StreamID: "stream-1", Sequence: 0, Type: "message.created", ReplaySafe: true})
	if _, err := r.Seal(SealRequest{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("duplicate sequence must fail closed, got %v", err)
	}
}
