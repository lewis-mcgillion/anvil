// Package jobs contains the lifecycle and idempotency rules shared by
// asynchronous administration operations.
package jobs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sync"
	"time"
)

var (
	ErrInvalid  = errors.New("invalid job request")
	ErrConflict = errors.New("idempotency key conflict")
	ErrNotFound = errors.New("job not found")
	ErrFinished = errors.New("job has already finished")
)

type Kind string

const (
	Export   Kind = "export"
	Import   Kind = "import"
	Deletion Kind = "deletion"
)

type Status string

const (
	Queued     Status = "queued"
	Running    Status = "running"
	Cancelling Status = "cancelling"
	Cancelled  Status = "cancelled"
	Succeeded  Status = "succeeded"
	Failed     Status = "failed"
)

type Job struct {
	ID        string
	ProjectID string
	Kind      Kind
	Status    Status
	Attempts  int
	CreatedAt time.Time
	Error     string
	Result    map[string]any
}

type Manager struct {
	mu     sync.RWMutex
	nextID uint64
	now    func() time.Time
	jobs   map[string]Job
	keys   map[string]idempotency
}

type idempotency struct {
	projectID string
	hash      string
	jobID     string
}

var keyPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{8,128}$`)

func New(now func() time.Time) *Manager {
	if now == nil {
		now = time.Now
	}
	return &Manager{now: now, jobs: make(map[string]Job), keys: make(map[string]idempotency)}
}

func (m *Manager) Create(ctx context.Context, projectID string, kind Kind, key string, payload any) (Job, error) {
	if err := ctx.Err(); err != nil {
		return Job{}, err
	}
	if projectID == "" || !validKind(kind) || !keyPattern.MatchString(key) {
		return Job{}, fmt.Errorf("%w: project, kind and idempotency key are required", ErrInvalid)
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return Job{}, fmt.Errorf("%w: payload is not serializable", ErrInvalid)
	}
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])

	m.mu.Lock()
	defer m.mu.Unlock()
	if prior, ok := m.keys[projectID+"\x00"+key]; ok {
		if prior.hash != hash {
			return Job{}, ErrConflict
		}
		return clone(m.jobs[prior.jobID]), nil
	}
	m.nextID++
	job := Job{
		ID: fmt.Sprintf("job-%d", m.nextID), ProjectID: projectID, Kind: kind,
		Status: Queued, CreatedAt: m.now(),
	}
	m.jobs[job.ID] = job
	m.keys[projectID+"\x00"+key] = idempotency{projectID: projectID, hash: hash, jobID: job.ID}
	return clone(job), nil
}

func (m *Manager) Get(projectID, id string) (Job, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	job, ok := m.jobs[id]
	if !ok || job.ProjectID != projectID {
		return Job{}, ErrNotFound
	}
	return clone(job), nil
}

func (m *Manager) Cancel(ctx context.Context, projectID, id string) (Job, error) {
	if err := ctx.Err(); err != nil {
		return Job{}, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	job, ok := m.jobs[id]
	if !ok || job.ProjectID != projectID {
		return Job{}, ErrNotFound
	}
	switch job.Status {
	case Queued, Running, Cancelling, Cancelled:
		if job.Status == Queued || job.Status == Running {
			job.Status = Cancelling
			m.jobs[id] = job
		}
		return clone(job), nil
	default:
		return Job{}, ErrFinished
	}
}

func (m *Manager) Finish(ctx context.Context, projectID, id string, status Status, result map[string]any, failure string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if status != Succeeded && status != Failed && status != Cancelled {
		return fmt.Errorf("%w: terminal status required", ErrInvalid)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	job, ok := m.jobs[id]
	if !ok || job.ProjectID != projectID {
		return ErrNotFound
	}
	if job.Status == Cancelled || job.Status == Succeeded || job.Status == Failed {
		return ErrFinished
	}
	if job.Status == Cancelling {
		status = Cancelled
		result = nil
		failure = ""
	}
	job.Status, job.Result, job.Error = status, cloneResult(result), failure
	m.jobs[id] = job
	return nil
}

func validKind(kind Kind) bool {
	return kind == Export || kind == Import || kind == Deletion
}

func clone(job Job) Job {
	job.Result = cloneResult(job.Result)
	return job
}

func cloneResult(result map[string]any) map[string]any {
	if result == nil {
		return nil
	}
	copy := make(map[string]any, len(result))
	for key, value := range result {
		copy[key] = value
	}
	return copy
}
