package events

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
)

var (
	ErrResumeUnavailable = errors.New("event resume point is no longer available")
	ErrFeedClosed        = errors.New("event feed is closed")
)

// EventFeed keeps a bounded immutable history and broadcasts new events to
// watchers. The history makes Last-Event-ID reconnects independent of timing.
type EventFeed struct {
	mu          sync.Mutex
	runID       string
	history     []Event
	capacity    int
	nextID      uint64
	subscribers map[uint64]chan Event
	closed      bool
}

func NewEventFeed(runID string, capacity int) (*EventFeed, error) {
	if runID == "" || capacity < 1 {
		return nil, fmt.Errorf("%w: run and positive capacity are required", ErrInvalid)
	}
	return &EventFeed{runID: runID, capacity: capacity, subscribers: make(map[uint64]chan Event)}, nil
}

// Publish records and broadcasts an event. Events for another run are
// rejected, and a slow watcher is disconnected rather than losing an event.
func (f *EventFeed) Publish(event Event) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed {
		return ErrFeedClosed
	}
	if event.RunID != f.runID || event.EventID == "" {
		return fmt.Errorf("%w: event does not belong to feed", ErrInvalid)
	}
	if len(f.history) == f.capacity {
		f.history = f.history[1:]
	}
	f.history = append(f.history, cloneEvent(event))
	for id, subscriber := range f.subscribers {
		select {
		case subscriber <- cloneEvent(event):
		default:
			close(subscriber)
			delete(f.subscribers, id)
		}
	}
	return nil
}

// Subscribe returns historical events after lastEventID followed by live
// events. An empty lastEventID starts at the oldest retained event.
func (f *EventFeed) Subscribe(ctx context.Context, lastEventID string) (<-chan Event, error) {
	if ctx == nil {
		return nil, fmt.Errorf("%w: nil context", ErrInvalid)
	}
	f.mu.Lock()
	if f.closed {
		f.mu.Unlock()
		return nil, ErrFeedClosed
	}
	start := 0
	if lastEventID != "" {
		found := false
		for i := range f.history {
			if f.history[i].EventID == lastEventID {
				start, found = i+1, true
				break
			}
		}
		if !found {
			f.mu.Unlock()
			return nil, ErrResumeUnavailable
		}
	}
	ch := make(chan Event, len(f.history)-start+1)
	for _, event := range f.history[start:] {
		ch <- cloneEvent(event)
	}
	f.nextID++
	subscriptionID := f.nextID
	f.subscribers[subscriptionID] = ch
	f.mu.Unlock()

	go func() {
		<-ctx.Done()
		f.mu.Lock()
		if subscriber, ok := f.subscribers[subscriptionID]; ok {
			delete(f.subscribers, subscriptionID)
			close(subscriber)
		}
		f.mu.Unlock()
	}()
	return ch, nil
}

func (f *EventFeed) Close() {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed {
		return
	}
	f.closed = true
	for id, subscriber := range f.subscribers {
		delete(f.subscribers, id)
		close(subscriber)
	}
}

// SSEFrame encodes one event using the stable event identity as its SSE id.
func SSEFrame(event Event) ([]byte, error) {
	data, err := json.Marshal(event)
	if err != nil {
		return nil, err
	}
	var b strings.Builder
	b.WriteString("id: ")
	b.WriteString(event.EventID)
	b.WriteString("\nevent: ")
	b.WriteString(event.Type)
	b.WriteString("\ndata: ")
	b.Write(data)
	b.WriteString("\n\n")
	return []byte(b.String()), nil
}

// ServeHTTP exposes the feed as an SSE response. Authentication and run
// authorization remain the responsibility of the API layer that mounts it.
func (f *EventFeed) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	stream, err := f.Subscribe(r.Context(), r.Header.Get("Last-Event-ID"))
	if err != nil {
		if errors.Is(err, ErrResumeUnavailable) {
			http.Error(w, "resume point is no longer available", http.StatusBadRequest)
			return
		}
		http.Error(w, "event feed is unavailable", http.StatusNotFound)
		return
	}
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Content-Type", "text/event-stream")
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming is unsupported", http.StatusInternalServerError)
		return
	}
	for {
		select {
		case <-r.Context().Done():
			return
		case event, ok := <-stream:
			if !ok {
				return
			}
			frame, err := SSEFrame(event)
			if err != nil {
				return
			}
			if _, err := w.Write(frame); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

// EventSequence is useful to callers that need to log a compact, human
// readable position without treating sequence as a globally ordered cursor.
func EventSequence(event Event) string {
	return event.StreamID + ":" + strconv.FormatInt(event.Sequence, 10)
}

func cloneEvent(event Event) Event {
	event.ParentEventIDs = append([]string(nil), event.ParentEventIDs...)
	event.Payload = append([]byte(nil), event.Payload...)
	return event
}
