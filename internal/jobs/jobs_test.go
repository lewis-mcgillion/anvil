package jobs

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestCreateIsIdempotentAndProjectScoped(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	m := New(func() time.Time { return now })
	first, err := m.Create(context.Background(), "project-a", Export, "request-1", map[string]any{"redact": true})
	if err != nil {
		t.Fatal(err)
	}
	retry, err := m.Create(context.Background(), "project-a", Export, "request-1", map[string]any{"redact": true})
	if err != nil || retry.ID != first.ID {
		t.Fatalf("retry = %+v, %v; want original job", retry, err)
	}
	if _, err := m.Create(context.Background(), "project-a", Export, "request-1", map[string]any{"redact": false}); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflicting retry error = %v, want conflict", err)
	}
	if _, err := m.Get("project-b", first.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross-project lookup error = %v, want not found", err)
	}
}

func TestCancellationIsIdempotentAndPreventsCompletion(t *testing.T) {
	m := New(nil)
	job, err := m.Create(context.Background(), "project-a", Import, "request-2", struct{}{})
	if err != nil {
		t.Fatal(err)
	}
	cancelled, err := m.Cancel(context.Background(), "project-a", job.ID)
	if err != nil || cancelled.Status != Cancelling {
		t.Fatalf("cancel = %+v, %v; want cancelling", cancelled, err)
	}
	if err := m.Finish(context.Background(), "project-a", job.ID, Succeeded, map[string]any{"archive": "partial"}, ""); err != nil {
		t.Fatal(err)
	}
	final, err := m.Get("project-a", job.ID)
	if err != nil || final.Status != Cancelled || final.Result != nil {
		t.Fatalf("final = %+v, %v; want cancelled without result", final, err)
	}
	repeat, err := m.Cancel(context.Background(), "project-a", job.ID)
	if err != nil || repeat.Status != Cancelled {
		t.Fatalf("repeat cancel = %+v, %v; want cancelled", repeat, err)
	}
}
