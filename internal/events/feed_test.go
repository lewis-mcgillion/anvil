package events

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestEventFeedReplaysAfterLastEventID(t *testing.T) {
	feed, err := NewEventFeed("run", 4)
	if err != nil {
		t.Fatal(err)
	}
	first := testEvent(0, `{"value":0}`)
	first.RunID = "run"
	first.EventID = "event-0"
	second := testEvent(1, `{"value":1}`)
	second.RunID = "run"
	second.EventID = "event-1"
	if err := feed.Publish(first); err != nil {
		t.Fatal(err)
	}
	if err := feed.Publish(second); err != nil {
		t.Fatal(err)
	}
	ch, err := feed.Subscribe(context.Background(), first.EventID)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-ch:
		if got.EventID != second.EventID {
			t.Fatalf("event = %q, want %q", got.EventID, second.EventID)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for replay")
	}
}

func TestEventFeedRejectsExpiredResumeAndClosesOnCancel(t *testing.T) {
	feed, err := NewEventFeed("run", 1)
	if err != nil {
		t.Fatal(err)
	}
	for i := int64(0); i < 2; i++ {
		event := testEvent(i, `{}`)
		event.RunID = "run"
		event.EventID = "event-" + string(rune('0'+i))
		if err := feed.Publish(event); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := feed.Subscribe(context.Background(), "event-0"); !errors.Is(err, ErrResumeUnavailable) {
		t.Fatalf("resume error = %v, want unavailable", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	ch, err := feed.Subscribe(ctx, "")
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	select {
	case _, ok := <-ch:
		if ok {
			// The retained event is delivered before cancellation is observed.
			for range ch {
			}
		}
	case <-time.After(time.Second):
		t.Fatal("subscription was not closed")
	}
}

func TestSSEFrameUsesEventIdentity(t *testing.T) {
	event := testEvent(0, `{"ok":true}`)
	event.EventID = "stable-id"
	frame, err := SSEFrame(event)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(frame), "id: stable-id\nevent: message.created\ndata: ") || !strings.HasSuffix(string(frame), "\n\n") {
		t.Fatalf("unexpected frame: %q", frame)
	}
}

func TestEventFeedHTTPResume(t *testing.T) {
	feed, err := NewEventFeed("run", 2)
	if err != nil {
		t.Fatal(err)
	}
	event := testEvent(0, `{"ok":true}`)
	event.RunID, event.EventID = "run", "stable-id"
	if err := feed.Publish(event); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/watch", nil)
	request.Header.Set("Last-Event-ID", event.EventID)
	response := &safeResponseWriter{header: make(http.Header)}
	ctx, cancel := context.WithCancel(request.Context())
	request = request.WithContext(ctx)
	go feed.ServeHTTP(response, request)
	time.Sleep(10 * time.Millisecond)
	next := testEvent(1, `{}`)
	next.RunID, next.EventID = "run", "next-id"
	if err := feed.Publish(next); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100 && !strings.Contains(response.body(), "next-id"); i++ {
		time.Sleep(time.Millisecond)
	}
	cancel()
	if !strings.Contains(response.body(), "id: next-id") {
		t.Fatalf("response = %q, want resumed event", response.body())
	}
}

type safeResponseWriter struct {
	mu       sync.Mutex
	header   http.Header
	bodyData strings.Builder
}

func (w *safeResponseWriter) Header() http.Header { return w.header }
func (w *safeResponseWriter) Write(data []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.bodyData.Write(data)
}
func (w *safeResponseWriter) WriteHeader(int) {}
func (w *safeResponseWriter) Flush()          {}
func (w *safeResponseWriter) body() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.bodyData.String()
}
