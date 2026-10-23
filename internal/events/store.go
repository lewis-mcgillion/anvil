// Package events persists immutable capture events.
package events

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

const (
	CurrentSchemaVersion = "1"
	MaxEventBytes        = 256 * 1024
	MaxBatchEvents       = 500
)

var (
	ErrConflict = errors.New("event conflicts with immutable stored content")
	ErrSequence = errors.New("event sequence is not the next sequence in the stream")
	ErrInvalid  = errors.New("invalid event")
)

// Event is the versioned wire representation stored in PostgreSQL JSONB.
// Payload is retained as JSON rather than decoded into an arbitrary object.
type Event struct {
	SchemaVersion  string          `json:"schema_version"`
	EventID        string          `json:"event_id"`
	RunID          string          `json:"run_id"`
	StreamID       string          `json:"stream_id"`
	Sequence       int64           `json:"sequence"`
	Type           string          `json:"type"`
	OccurredAt     time.Time       `json:"occurred_at"`
	TraceID        string          `json:"trace_id,omitempty"`
	SpanID         string          `json:"span_id,omitempty"`
	ParentEventIDs []string        `json:"parent_event_ids"`
	InvocationID   string          `json:"invocation_id,omitempty"`
	Payload        json.RawMessage `json:"payload"`
}

type BatchResult struct {
	Accepted   int
	Duplicates int
}

// Store uses a transaction per batch. The stream row is locked before any
// event is inspected or written, so a failed batch cannot partially commit.
type Store struct{ DB *sql.DB }

func (s *Store) AcceptBatch(ctx context.Context, projectID, runID, streamID string, batch []Event) (BatchResult, error) {
	if s == nil || s.DB == nil {
		return BatchResult{}, fmt.Errorf("%w: nil database", ErrInvalid)
	}
	if err := ValidateBatch(projectID, runID, streamID, batch); err != nil {
		return BatchResult{}, err
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return BatchResult{}, err
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback()
		}
	}()

	var next int64
	err = tx.QueryRowContext(ctx, `SELECT next_sequence FROM run_streams
		WHERE project_id = $1 AND run_id = $2 AND stream_id = $3 FOR UPDATE`,
		projectID, runID, streamID).Scan(&next)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return BatchResult{}, fmt.Errorf("%w: stream does not exist", ErrInvalid)
		}
		return BatchResult{}, err
	}

	result := BatchResult{}
	for _, event := range batch {
		hash, err := ContentHash(event)
		if err != nil {
			return BatchResult{}, err
		}
		var existing []byte
		err = tx.QueryRowContext(ctx, `SELECT content_hash FROM events
			WHERE project_id = $1 AND event_id = $2`, projectID, event.EventID).Scan(&existing)
		switch {
		case err == nil:
			if !equalBytes(existing, hash[:]) {
				return BatchResult{}, fmt.Errorf("%w: event_id %s", ErrConflict, event.EventID)
			}
			result.Duplicates++
			continue
		case !errors.Is(err, sql.ErrNoRows):
			return BatchResult{}, err
		}
		if event.Sequence != next {
			return BatchResult{}, fmt.Errorf("%w: got %d, want %d", ErrSequence, event.Sequence, next)
		}
		parents, _ := json.Marshal(event.ParentEventIDs)
		_, err = tx.ExecContext(ctx, `INSERT INTO events
			(event_id, project_id, run_id, stream_id, sequence, schema_version,
			 event_type, occurred_at, trace_id, span_id, parent_event_ids,
			 invocation_id, payload, content_hash)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
			event.EventID, projectID, event.RunID, event.StreamID, event.Sequence,
			event.SchemaVersion, event.Type, event.OccurredAt, event.TraceID,
			event.SpanID, parents, nullUUID(event.InvocationID), event.Payload, hash[:])
		if err != nil {
			return BatchResult{}, err
		}
		next++
		result.Accepted++
	}
	if result.Accepted > 0 {
		if _, err = tx.ExecContext(ctx, `UPDATE run_streams SET next_sequence = $1
			WHERE project_id = $2 AND stream_id = $3`, next, projectID, streamID); err != nil {
			return BatchResult{}, err
		}
	}
	if err = tx.Commit(); err != nil {
		return BatchResult{}, err
	}
	committed = true
	return result, nil
}

func ValidateBatch(projectID, runID, streamID string, batch []Event) error {
	if projectID == "" || runID == "" || streamID == "" || len(batch) == 0 || len(batch) > MaxBatchEvents {
		return fmt.Errorf("%w: missing scope or invalid batch size", ErrInvalid)
	}
	seen := make(map[string]struct{}, len(batch))
	for _, event := range batch {
		if event.SchemaVersion != CurrentSchemaVersion || event.EventID == "" ||
			event.RunID != runID || event.StreamID != streamID || event.Sequence < 0 ||
			event.Type == "" || event.OccurredAt.IsZero() || len(event.Payload) == 0 ||
			!json.Valid(event.Payload) {
			return fmt.Errorf("%w: malformed event %s", ErrInvalid, event.EventID)
		}
		if len(eventBytes(event)) > MaxEventBytes {
			return fmt.Errorf("%w: event %s exceeds %d bytes", ErrInvalid, event.EventID, MaxEventBytes)
		}
		if _, ok := seen[event.EventID]; ok {
			return fmt.Errorf("%w: duplicate event identity in batch", ErrConflict)
		}
		seen[event.EventID] = struct{}{}
	}
	return nil
}

func ContentHash(event Event) ([32]byte, error) {
	b, err := canonicalEvent(event)
	if err != nil {
		return [32]byte{}, err
	}
	return sha256.Sum256(b), nil
}

func ContentHashHex(event Event) (string, error) {
	hash, err := ContentHash(event)
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(hash[:]), nil
}

func canonicalEvent(event Event) ([]byte, error) {
	return canonicalJSON(map[string]any{
		"schema_version": event.SchemaVersion, "event_id": event.EventID,
		"run_id": event.RunID, "stream_id": event.StreamID, "sequence": event.Sequence,
		"type": event.Type, "occurred_at": event.OccurredAt.UTC().Format(time.RFC3339Nano),
		"trace_id": event.TraceID, "span_id": event.SpanID, "parent_event_ids": event.ParentEventIDs,
		"invocation_id": event.InvocationID, "payload": json.RawMessage(event.Payload),
	})
}

func canonicalJSON(value any) ([]byte, error) {
	switch v := value.(type) {
	case json.RawMessage:
		var decoded any
		if err := json.Unmarshal(v, &decoded); err != nil {
			return nil, err
		}
		return canonicalJSON(decoded)
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		var b strings.Builder
		b.WriteByte('{')
		for i, key := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			keyJSON, _ := json.Marshal(key)
			b.Write(keyJSON)
			b.WriteByte(':')
			child, err := canonicalJSON(v[key])
			if err != nil {
				return nil, err
			}
			b.Write(child)
		}
		b.WriteByte('}')
		return []byte(b.String()), nil
	case []any:
		var b strings.Builder
		b.WriteByte('[')
		for i, child := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			encoded, err := canonicalJSON(child)
			if err != nil {
				return nil, err
			}
			b.Write(encoded)
		}
		b.WriteByte(']')
		return []byte(b.String()), nil
	default:
		return json.Marshal(v)
	}
}

func eventBytes(event Event) []byte {
	b, _ := json.Marshal(event)
	return b
}

func equalBytes(a, b []byte) bool {
	return len(a) == len(b) && string(a) == string(b)
}

func nullUUID(value string) any {
	if value == "" {
		return nil
	}
	return value
}
