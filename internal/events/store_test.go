package events

import (
	"errors"
	"testing"
	"time"
)

func testEvent(sequence int64, payload string) Event {
	return Event{
		SchemaVersion: CurrentSchemaVersion, EventID: "event-" + string(rune('a'+sequence)),
		RunID: "run", StreamID: "stream", Sequence: sequence, Type: "message.created",
		OccurredAt: time.Unix(1, sequence).UTC(), ParentEventIDs: []string{},
		Payload: []byte(payload),
	}
}

func TestValidateBatchAcceptsVersionedEvents(t *testing.T) {
	if err := ValidateBatch("project", "run", "stream", []Event{testEvent(0, `{"b":2,"a":1}`)}); err != nil {
		t.Fatalf("ValidateBatch() error = %v", err)
	}
}

func TestValidateBatchRejectsPartialBatchInputs(t *testing.T) {
	batch := []Event{testEvent(0, `{}`), testEvent(2, `{}`)}
	batch[1].EventID = batch[0].EventID
	err := ValidateBatch("project", "run", "stream", batch)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("error = %v, want conflict", err)
	}
}

func TestContentHashCanonicalizesObjectKeyOrder(t *testing.T) {
	left := testEvent(0, `{"a":1,"nested":{"x":true,"y":"z"}}`)
	right := testEvent(0, `{"nested":{"y":"z","x":true},"a":1}`)
	a, err := ContentHashHex(left)
	if err != nil {
		t.Fatal(err)
	}
	b, err := ContentHashHex(right)
	if err != nil {
		t.Fatal(err)
	}
	if a != b {
		t.Fatalf("hashes differ: %s != %s", a, b)
	}
}

func TestValidateBatchRejectsOversizedBatch(t *testing.T) {
	event := testEvent(0, `{"value":"`+string(make([]byte, MaxEventBytes))+`"}`)
	if err := ValidateBatch("project", "run", "stream", []Event{event}); err == nil {
		t.Fatal("ValidateBatch() accepted an oversized event")
	}
}
