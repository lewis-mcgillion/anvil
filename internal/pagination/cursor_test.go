package pagination

import (
	"errors"
	"testing"
)

func testCodec(t *testing.T) *Codec {
	t.Helper()
	codec, err := NewCodec([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatal(err)
	}
	return codec
}

func TestCursorRoundTripBindsFilters(t *testing.T) {
	codec := testCodec(t)
	want := Cursor{Version: CurrentVersion, Scope: "project-1", Resource: "runs", Key: "created:2026-01-01T00:00:00Z,id:run-1", Filters: map[string]string{"state": "failed"}}
	value, err := codec.Encode(want)
	if err != nil {
		t.Fatal(err)
	}
	got, err := codec.Decode(value)
	if err != nil {
		t.Fatal(err)
	}
	if got.Version != want.Version || got.Scope != want.Scope || got.Resource != want.Resource || got.Key != want.Key || got.Filters["state"] != "failed" {
		t.Fatalf("decoded cursor = %#v, want %#v", got, want)
	}
}

func TestCursorRejectsTamperingAndWrongKey(t *testing.T) {
	codec := testCodec(t)
	value, err := codec.Encode(Cursor{Version: CurrentVersion, Scope: "project-1", Resource: "events", Key: "event-1"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := codec.Decode(value[:len(value)-1] + "A"); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("tampered cursor error = %v, want invalid cursor", err)
	}
	other, _ := NewCodec([]byte("fedcba9876543210fedcba9876543210"))
	if _, err := other.Decode(value); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("wrong key error = %v, want invalid cursor", err)
	}
}

func TestCursorRejectsUnsupportedVersion(t *testing.T) {
	codec := testCodec(t)
	if _, err := codec.Encode(Cursor{Version: 2, Scope: "p", Resource: "runs", Key: "k"}); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("error = %v, want invalid cursor", err)
	}
}
