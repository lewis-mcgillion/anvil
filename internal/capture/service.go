// Package capture contains the bounded, project-scoped run capture service.
package capture

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/lewis-mcgillion/anvil/internal/events"
	"github.com/lewis-mcgillion/anvil/internal/sealing"
	"github.com/lewis-mcgillion/anvil/internal/storage"
)

const (
	CaptureWrite = "capture:write"
	RunsRead     = "runs:read"
)

var (
	ErrUnauthorized   = errors.New("unauthenticated")
	ErrForbidden      = errors.New("forbidden")
	ErrInvalidRequest = errors.New("invalid request")
	ErrNotFound       = errors.New("not found")
	ErrConflict       = errors.New("conflict")
	ErrTooLarge       = errors.New("payload too large")
	ErrThrottled      = errors.New("quota exceeded")
)

type Run struct {
	ID             string     `json:"id"`
	ProjectID      string     `json:"project_id"`
	CaptureState   string     `json:"capture_state"`
	ExecutionState string     `json:"execution_state"`
	AgentName      string     `json:"agent_name"`
	Adapter        string     `json:"adapter"`
	AdapterVersion string     `json:"adapter_version"`
	TraceID        string     `json:"trace_id,omitempty"`
	StartedAt      time.Time  `json:"started_at"`
	EndedAt        *time.Time `json:"ended_at"`
}

type CreateRunRequest struct {
	AgentName      string    `json:"agent_name"`
	Adapter        string    `json:"adapter"`
	AdapterVersion string    `json:"adapter_version"`
	TraceID        string    `json:"trace_id,omitempty"`
	StartedAt      time.Time `json:"started_at,omitempty"`
}

type EventBatch struct {
	Events []events.Event `json:"events"`
}

type EventBatchResult struct {
	Accepted   int `json:"accepted"`
	Duplicates int `json:"duplicates"`
}

type ArtifactRequest struct {
	Name        string `json:"name"`
	ContentType string `json:"content_type"`
	Content     string `json:"content_base64"`
}

type ArtifactResponse struct {
	ID          string    `json:"id"`
	RunID       string    `json:"run_id"`
	Name        string    `json:"name"`
	ContentType string    `json:"content_type"`
	SizeBytes   int       `json:"size_bytes"`
	SHA256      string    `json:"sha256"`
	CreatedAt   time.Time `json:"created_at"`
}

type Job struct {
	ID     string `json:"job_id"`
	Status string `json:"status"`
	URL    string `json:"job_url"`
}

type serviceRun struct {
	run       Run
	events    []events.Event
	streams   map[string]bool
	artifacts int64
	sealed    *sealing.Manifest
}

type Service struct {
	mu          sync.Mutex
	runs        map[string]*serviceRun
	jobs        map[string]Job
	idempotency map[string]idempotencyEntry
	artifacts   *storage.ArtifactStore
	maxRunBytes int64
}

type idempotencyEntry struct {
	request  []byte
	response []byte
}

func NewService() *Service {
	return &Service{
		runs: make(map[string]*serviceRun), jobs: make(map[string]Job),
		idempotency: make(map[string]idempotencyEntry), artifacts: storage.NewArtifactStore(),
		maxRunBytes: 64 << 20,
	}
}

func RequireScope(scopes map[string]bool, scope string) error {
	if scopes == nil {
		return ErrUnauthorized
	}
	if !scopes[scope] {
		return ErrForbidden
	}
	return nil
}

func (s *Service) CreateRun(projectID, key string, req CreateRunRequest, scopes map[string]bool) (Run, error) {
	if err := RequireScope(scopes, CaptureWrite); err != nil {
		return Run{}, err
	}
	if projectID == "" || req.AgentName == "" || req.Adapter == "" || req.AdapterVersion == "" {
		return Run{}, fmt.Errorf("%w: invalid run", ErrConflict)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if key != "" {
		requestBytes, _ := json.Marshal(req)
		if cached, ok := s.idempotency[key]; ok {
			if string(cached.request) != string(requestBytes) {
				return Run{}, ErrConflict
			}
			var run Run
			if json.Unmarshal(cached.response, &run) == nil {
				return run, nil
			}
		}
	}
	started := req.StartedAt
	if started.IsZero() {
		started = time.Now().UTC()
	}
	run := Run{ID: newID(), ProjectID: projectID, CaptureState: "open", ExecutionState: "running",
		AgentName: req.AgentName, Adapter: req.Adapter, AdapterVersion: req.AdapterVersion,
		TraceID: req.TraceID, StartedAt: started}
	s.runs[run.ID] = &serviceRun{run: run, streams: make(map[string]bool)}
	if key != "" {
		if b, err := json.Marshal(run); err == nil {
			requestBytes, _ := json.Marshal(req)
			s.idempotency[key] = idempotencyEntry{request: requestBytes, response: b}
		}
	}
	return run, nil
}

func (s *Service) Ingest(runID, key string, batch EventBatch, scopes map[string]bool) (EventBatchResult, error) {
	if err := RequireScope(scopes, CaptureWrite); err != nil {
		return EventBatchResult{}, err
	}
	if len(batch.Events) == 0 || len(batch.Events) > events.MaxBatchEvents {
		return EventBatchResult{}, ErrTooLarge
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.runs[runID]
	if !ok {
		return EventBatchResult{}, ErrNotFound
	}
	if r.run.CaptureState != "open" {
		return EventBatchResult{}, ErrConflict
	}
	if key != "" {
		requestBytes, _ := json.Marshal(batch)
		if cached, ok := s.idempotency[key]; ok {
			if string(cached.request) != string(requestBytes) {
				return EventBatchResult{}, ErrConflict
			}
			var result EventBatchResult
			_ = json.Unmarshal(cached.response, &result)
			return result, nil
		}
	}
	byID := make(map[string]events.Event, len(r.events))
	for _, event := range r.events {
		byID[event.EventID] = event
	}
	var size int64
	seenBatch := make(map[string]struct{}, len(batch.Events))
	for _, event := range batch.Events {
		if _, exists := seenBatch[event.EventID]; exists {
			return EventBatchResult{}, ErrConflict
		}
		seenBatch[event.EventID] = struct{}{}
		if event.RunID != runID {
			return EventBatchResult{}, ErrConflict
		}
		if err := events.ValidateBatch(r.run.ProjectID, runID, event.StreamID, []events.Event{event}); err != nil && !errors.Is(err, events.ErrSequence) {
			if len(event.Payload) > events.MaxEventBytes {
				return EventBatchResult{}, ErrTooLarge
			}
			return EventBatchResult{}, err
		}
		encoded, _ := json.Marshal(event)
		size += int64(len(encoded))
		if size > events.MaxBatchBytes {
			return EventBatchResult{}, ErrTooLarge
		}
		if existing, exists := byID[event.EventID]; exists {
			hashA, _ := events.ContentHash(existing)
			hashB, _ := events.ContentHash(event)
			if hashA != hashB {
				return EventBatchResult{}, ErrConflict
			}
		}
	}
	if r.artifacts+size > s.maxRunBytes {
		return EventBatchResult{}, ErrThrottled
	}
	result := EventBatchResult{}
	for _, event := range batch.Events {
		if _, exists := byID[event.EventID]; exists {
			result.Duplicates++
			continue
		}
		r.events = append(r.events, event)
		result.Accepted++
		terminal := event.Type == "run.completed" || event.Type == "run.failed"
		if terminal {
			r.streams[event.StreamID] = true
		} else if _, exists := r.streams[event.StreamID]; !exists {
			r.streams[event.StreamID] = false
		}
	}
	r.artifacts += size
	if key != "" {
		if b, err := json.Marshal(result); err == nil {
			requestBytes, _ := json.Marshal(batch)
			s.idempotency[key] = idempotencyEntry{request: requestBytes, response: b}
		}
	}
	return result, nil
}

func (s *Service) UploadArtifact(runID, key string, req ArtifactRequest, scopes map[string]bool) (ArtifactResponse, error) {
	if err := RequireScope(scopes, CaptureWrite); err != nil {
		return ArtifactResponse{}, err
	}
	data, err := base64.StdEncoding.DecodeString(req.Content)
	if err != nil {
		return ArtifactResponse{}, fmt.Errorf("%w: invalid base64", ErrConflict)
	}
	if len(data) > int(storage.MaxArtifactSize) {
		return ArtifactResponse{}, ErrTooLarge
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.runs[runID]
	if !ok {
		return ArtifactResponse{}, ErrNotFound
	}
	requestBytes, _ := json.Marshal(req)
	if key != "" {
		if cached, ok := s.idempotency[key]; ok {
			if string(cached.request) != string(requestBytes) {
				return ArtifactResponse{}, ErrConflict
			}
			var response ArtifactResponse
			if json.Unmarshal(cached.response, &response) == nil {
				return response, nil
			}
		}
		if r.run.CaptureState != "open" {
			return ArtifactResponse{}, ErrConflict
		}
	}
	if r.artifacts+int64(len(data)) > s.maxRunBytes {
		return ArtifactResponse{}, ErrThrottled
	}
	artifact, err := s.artifacts.Put(runID, req.Name, req.ContentType, data)
	if err != nil {
		return ArtifactResponse{}, err
	}
	r.artifacts += int64(len(data))
	response := ArtifactResponse{ID: newID(), RunID: runID, Name: req.Name, ContentType: req.ContentType,
		SizeBytes: len(data), SHA256: artifact.Hash, CreatedAt: time.Now().UTC()}
	if key != "" {
		if responseBytes, marshalErr := json.Marshal(response); marshalErr == nil {
			s.idempotency[key] = idempotencyEntry{request: requestBytes, response: responseBytes}
		}
	}
	return response, nil
}

func (s *Service) Seal(runID, key string, acknowledge bool, scopes map[string]bool) (Job, error) {
	if err := RequireScope(scopes, CaptureWrite); err != nil {
		return Job{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.runs[runID]
	if !ok {
		return Job{}, ErrNotFound
	}
	requestBytes, _ := json.Marshal(acknowledge)
	if key != "" {
		if cached, ok := s.idempotency[key]; ok {
			if string(cached.request) != string(requestBytes) {
				return Job{}, ErrConflict
			}
			var job Job
			if json.Unmarshal(cached.response, &job) == nil {
				return job, nil
			}
		}
		if r.run.CaptureState != "open" {
			return Job{}, ErrConflict
		}
	}
	r.run.CaptureState = "sealing"
	jobID := newID()
	job := Job{ID: jobID, Status: "queued", URL: "/v1/jobs/" + jobID}
	s.jobs[job.ID] = job
	if key != "" {
		if responseBytes, marshalErr := json.Marshal(job); marshalErr == nil {
			s.idempotency[key] = idempotencyEntry{request: requestBytes, response: responseBytes}
		}
	}
	go s.finishSeal(job.ID, runID, acknowledge)
	return job, nil
}

func (s *Service) finishSeal(jobID, runID string, acknowledge bool) {
	s.mu.Lock()
	r := s.runs[runID]
	model := sealing.Run{ID: r.run.ID, ProjectID: r.run.ProjectID}
	for stream, terminal := range r.streams {
		_ = model.AddStream(sealing.Stream{ID: stream, Terminal: terminal})
	}
	for _, event := range r.events {
		_ = model.AddEvent(sealing.Event{ID: event.EventID, StreamID: event.StreamID, Sequence: int(event.Sequence), Type: event.Type, ParentEventIDs: event.ParentEventIDs, InvocationID: event.InvocationID, ReplaySafe: true})
	}
	s.mu.Unlock()
	result, err := model.Seal(sealing.SealRequest{AcknowledgeIncomplete: acknowledge})
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		r.run.CaptureState = "open"
		s.jobs[jobID] = Job{ID: jobID, Status: "failed", URL: "/v1/jobs/" + jobID}
		return
	}
	r.sealed = &result.Manifest
	r.run.CaptureState = string(result.Manifest.CaptureCompleteness)
	s.jobs[jobID] = Job{ID: jobID, Status: "succeeded", URL: "/v1/jobs/" + jobID}
}

func (s *Service) Job(id string, scopes map[string]bool) (Job, error) {
	if err := RequireScope(scopes, RunsRead); err != nil {
		return Job{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	job, ok := s.jobs[id]
	if !ok {
		return Job{}, ErrNotFound
	}
	return job, nil
}

func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x70
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func ParseScopes(header string) map[string]bool {
	const prefix = "Bearer "
	if !strings.HasPrefix(header, prefix) {
		return nil
	}
	scopes := map[string]bool{}
	for _, value := range strings.Fields(strings.ReplaceAll(header[len(prefix):], ",", " ")) {
		scopes[value] = true
	}
	return scopes
}

func Problem(w http.ResponseWriter, status int, code string, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"type": "https://anvil.dev/problems/" + code, "title": http.StatusText(status), "status": status, "detail": detail, "code": code})
}
