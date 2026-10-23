// Package retention contains the project-scoped data lifecycle rules.
package retention

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

const DefaultRetention = 30 * 24 * time.Hour

var (
	ErrQuotaExceeded = errors.New("project storage quota exceeded")
	ErrNotFound      = errors.New("resource not found")
	ErrConflict      = errors.New("resource already exists")
	ErrInvalid       = errors.New("invalid lifecycle request")
)

type Kind string

const (
	Run          Kind = "run"
	Stream       Kind = "run_stream"
	Event        Kind = "event"
	Artifact     Kind = "artifact"
	Recording    Kind = "recording"
	Reexecution  Kind = "reexecution"
	Comparison   Kind = "comparison"
	SuiteRun     Kind = "suite_run"
	OtherDerived Kind = "derived"
)

type Resource struct {
	ID        string
	ProjectID string
	Kind      Kind
	Bytes     int64
	CreatedAt time.Time
	Parents   []string
	Protected bool
}

type Quota struct {
	MaxBytes int64
}

type Policy struct {
	Retention time.Duration
}

type Backup struct {
	ID        string
	ProjectID string
	Bytes     int64
	CreatedAt time.Time
	RetainFor time.Duration
}

type AuditEvent struct {
	ID          string
	ProjectID   string
	Kind        string
	RootIDs     []string
	ResourceIDs []string
	Bytes       int64
	Reason      string
	At          time.Time
}

type Manager struct {
	mu       sync.RWMutex
	now      func() time.Time
	nextID   uint64
	items    map[string]Resource
	backups  map[string]Backup
	quotas   map[string]Quota
	policies map[string]Policy
	audit    []AuditEvent
}

func New(now func() time.Time) *Manager {
	if now == nil {
		now = time.Now
	}
	return &Manager{
		now:      now,
		items:    make(map[string]Resource),
		backups:  make(map[string]Backup),
		quotas:   make(map[string]Quota),
		policies: make(map[string]Policy),
	}
}

func (m *Manager) SetQuota(projectID string, quota Quota) error {
	if projectID == "" || quota.MaxBytes < 0 {
		return fmt.Errorf("%w: project and non-negative quota are required", ErrInvalid)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.usageLocked(projectID)+0 > quota.MaxBytes {
		return fmt.Errorf("%w: existing usage is above the new limit", ErrQuotaExceeded)
	}
	m.quotas[projectID] = quota
	return nil
}

func (m *Manager) SetPolicy(projectID string, policy Policy) error {
	if projectID == "" {
		return fmt.Errorf("%w: project is required", ErrInvalid)
	}
	if policy.Retention == 0 {
		policy.Retention = DefaultRetention
	}
	if policy.Retention < 0 {
		return fmt.Errorf("%w: retention must not be negative", ErrInvalid)
	}
	m.mu.Lock()
	m.policies[projectID] = policy
	m.mu.Unlock()
	return nil
}

func (m *Manager) Put(ctx context.Context, item Resource) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if item.ID == "" || item.ProjectID == "" || item.Kind == "" || item.Bytes < 0 {
		return fmt.Errorf("%w: resource fields are invalid", ErrInvalid)
	}
	if item.CreatedAt.IsZero() {
		item.CreatedAt = m.now()
	}
	item.Parents = append([]string(nil), item.Parents...)

	m.mu.Lock()
	defer m.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	if _, exists := m.items[item.ID]; exists {
		return fmt.Errorf("%w: %s", ErrConflict, item.ID)
	}
	if item.Kind != Run {
		for _, parent := range item.Parents {
			parentItem, ok := m.items[parent]
			if !ok || parentItem.ProjectID != item.ProjectID {
				return fmt.Errorf("%w: parent %s is missing or out of project", ErrInvalid, parent)
			}
		}
	}
	if quota, ok := m.quotas[item.ProjectID]; ok && m.usageLocked(item.ProjectID)+item.Bytes > quota.MaxBytes {
		return fmt.Errorf("%w: limit %d bytes", ErrQuotaExceeded, quota.MaxBytes)
	}
	m.items[item.ID] = item
	return nil
}

func (m *Manager) Delete(ctx context.Context, projectID string, roots []string, reason string) (AuditEvent, error) {
	if projectID == "" || len(roots) == 0 {
		return AuditEvent{}, fmt.Errorf("%w: project and at least one root are required", ErrInvalid)
	}
	if err := ctx.Err(); err != nil {
		return AuditEvent{}, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	ids, bytes, err := m.graphLocked(ctx, projectID, roots)
	if err != nil {
		return AuditEvent{}, err
	}
	for _, id := range ids {
		delete(m.items, id)
	}
	event := m.newAuditLocked(projectID, "delete", roots, ids, bytes, reason)
	m.audit = append(m.audit, event)
	return cloneAudit(event), nil
}

func (m *Manager) Sweep(ctx context.Context, projectID string) ([]AuditEvent, error) {
	if projectID == "" {
		return nil, fmt.Errorf("%w: project is required", ErrInvalid)
	}
	policy := m.policy(projectID)
	cutoff := m.now().Add(-policy.Retention)

	m.mu.Lock()
	defer m.mu.Unlock()
	var roots []string
	for id, item := range m.items {
		if item.ProjectID == projectID && !item.Protected && !item.CreatedAt.After(cutoff) {
			roots = append(roots, id)
		}
	}
	if len(roots) == 0 {
		return nil, nil
	}
	sort.Strings(roots)
	ids, bytes, err := m.graphLocked(ctx, projectID, roots)
	if err != nil {
		return nil, err
	}
	for _, id := range ids {
		delete(m.items, id)
	}
	event := m.newAuditLocked(projectID, "retention", roots, ids, bytes, "retention expiry")
	m.audit = append(m.audit, event)
	return []AuditEvent{cloneAudit(event)}, nil
}

func (m *Manager) PutBackup(ctx context.Context, backup Backup) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if backup.ID == "" || backup.ProjectID == "" || backup.Bytes < 0 {
		return fmt.Errorf("%w: backup fields are invalid", ErrInvalid)
	}
	if backup.CreatedAt.IsZero() {
		backup.CreatedAt = m.now()
	}
	if backup.RetainFor == 0 {
		backup.RetainFor = DefaultRetention
	}
	if backup.RetainFor < 0 {
		return fmt.Errorf("%w: backup retention must not be negative", ErrInvalid)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, exists := m.backups[backup.ID]; exists {
		return fmt.Errorf("%w: %s", ErrConflict, backup.ID)
	}
	m.backups[backup.ID] = backup
	return nil
}

func (m *Manager) PruneBackups(ctx context.Context, projectID string) ([]string, error) {
	if projectID == "" {
		return nil, fmt.Errorf("%w: project is required", ErrInvalid)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	var removed []string
	now := m.now()
	for id, backup := range m.backups {
		if backup.ProjectID != projectID || now.Before(backup.CreatedAt.Add(backup.RetainFor)) {
			continue
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		delete(m.backups, id)
		removed = append(removed, id)
	}
	sort.Strings(removed)
	return removed, nil
}

func (m *Manager) Usage(projectID string) int64 {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.usageLocked(projectID)
}

func (m *Manager) Audit(projectID string) []AuditEvent {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var result []AuditEvent
	for _, event := range m.audit {
		if projectID == "" || event.ProjectID == projectID {
			result = append(result, cloneAudit(event))
		}
	}
	return result
}

func (m *Manager) policy(projectID string) Policy {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if policy, ok := m.policies[projectID]; ok {
		return policy
	}
	return Policy{Retention: DefaultRetention}
}

func (m *Manager) usageLocked(projectID string) int64 {
	var bytes int64
	for _, item := range m.items {
		if item.ProjectID == projectID {
			bytes += item.Bytes
		}
	}
	return bytes
}

func (m *Manager) graphLocked(ctx context.Context, projectID string, roots []string) ([]string, int64, error) {
	seen := make(map[string]bool)
	var visit func(string) error
	visit = func(id string) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if seen[id] {
			return nil
		}
		item, ok := m.items[id]
		if !ok {
			return fmt.Errorf("%w: %s", ErrNotFound, id)
		}
		if item.ProjectID != projectID {
			return fmt.Errorf("%w: resource %s is out of project", ErrInvalid, id)
		}
		seen[id] = true
		return nil
	}
	for _, root := range roots {
		if err := visit(root); err != nil {
			return nil, 0, err
		}
	}
	changed := true
	for changed {
		changed = false
		for id, item := range m.items {
			if item.ProjectID != projectID || seen[id] {
				continue
			}
			for _, parent := range item.Parents {
				if seen[parent] {
					if err := visit(id); err != nil {
						return nil, 0, err
					}
					changed = true
					break
				}
			}
		}
	}
	ids := make([]string, 0, len(seen))
	var bytes int64
	for id := range seen {
		ids = append(ids, id)
		bytes += m.items[id].Bytes
	}
	sort.Strings(ids)
	return ids, bytes, nil
}

func (m *Manager) newAuditLocked(projectID, kind string, roots, ids []string, bytes int64, reason string) AuditEvent {
	m.nextID++
	return AuditEvent{
		ID:          fmt.Sprintf("deletion-%d", m.nextID),
		ProjectID:   projectID,
		Kind:        kind,
		RootIDs:     append([]string(nil), roots...),
		ResourceIDs: append([]string(nil), ids...),
		Bytes:       bytes,
		Reason:      reason,
		At:          m.now(),
	}
}

func cloneAudit(event AuditEvent) AuditEvent {
	event.RootIDs = append([]string(nil), event.RootIDs...)
	event.ResourceIDs = append([]string(nil), event.ResourceIDs...)
	return event
}
