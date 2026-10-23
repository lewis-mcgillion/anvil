package retention

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestDeleteTraversesDerivedGraphAndAccountsStorage(t *testing.T) {
	now := time.Date(2026, 1, 31, 0, 0, 0, 0, time.UTC)
	m := New(func() time.Time { return now })
	if err := m.SetQuota("p", Quota{MaxBytes: 100}); err != nil {
		t.Fatal(err)
	}
	for _, item := range []Resource{
		{ID: "run", ProjectID: "p", Kind: Run, Bytes: 20, CreatedAt: now},
		{ID: "recording", ProjectID: "p", Kind: Recording, Bytes: 30, Parents: []string{"run"}, CreatedAt: now},
		{ID: "comparison", ProjectID: "p", Kind: Comparison, Bytes: 40, Parents: []string{"recording"}, CreatedAt: now},
	} {
		if err := m.Put(context.Background(), item); err != nil {
			t.Fatal(err)
		}
	}
	if got := m.Usage("p"); got != 90 {
		t.Fatalf("usage before delete = %d, want 90", got)
	}
	audit, err := m.Delete(context.Background(), "p", []string{"run"}, "user request")
	if err != nil {
		t.Fatal(err)
	}
	if audit.Bytes != 90 || len(audit.ResourceIDs) != 3 {
		t.Fatalf("unexpected audit evidence: %+v", audit)
	}
	if got := m.Usage("p"); got != 0 {
		t.Fatalf("usage after delete = %d, want 0", got)
	}
	if len(m.Audit("p")) != 1 {
		t.Fatal("deletion audit evidence was not retained")
	}
}

func TestQuotaAndCrossProjectReferencesFailWithoutPartialWrite(t *testing.T) {
	m := New(nil)
	if err := m.SetQuota("p", Quota{MaxBytes: 10}); err != nil {
		t.Fatal(err)
	}
	if err := m.Put(context.Background(), Resource{ID: "run", ProjectID: "p", Kind: Run, Bytes: 8}); err != nil {
		t.Fatal(err)
	}
	err := m.Put(context.Background(), Resource{ID: "too-large", ProjectID: "p", Kind: Artifact, Bytes: 3, Parents: []string{"run"}})
	if !errors.Is(err, ErrQuotaExceeded) || m.Usage("p") != 8 {
		t.Fatalf("quota rejection changed state: err=%v usage=%d", err, m.Usage("p"))
	}
	err = m.Put(context.Background(), Resource{ID: "foreign-child", ProjectID: "other", Kind: Artifact, Bytes: 1, Parents: []string{"run"}})
	if !errors.Is(err, ErrInvalid) {
		t.Fatalf("cross-project parent error = %v", err)
	}
}

func TestDefaultsSweepProtectedAndBackupRetention(t *testing.T) {
	now := time.Date(2026, 2, 1, 0, 0, 0, 0, time.UTC)
	m := New(func() time.Time { return now })
	old := now.Add(-DefaultRetention)
	for _, item := range []Resource{
		{ID: "expired", ProjectID: "p", Kind: Run, Bytes: 1, CreatedAt: old},
		{ID: "protected", ProjectID: "p", Kind: Run, Bytes: 1, CreatedAt: old, Protected: true},
	} {
		if err := m.Put(context.Background(), item); err != nil {
			t.Fatal(err)
		}
	}
	if err := m.PutBackup(context.Background(), Backup{ID: "backup", ProjectID: "p", Bytes: 1, CreatedAt: old}); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Sweep(context.Background(), "p"); err != nil {
		t.Fatal(err)
	}
	if m.Usage("p") != 1 {
		t.Fatalf("protected resource was removed or expired resource remained: usage=%d", m.Usage("p"))
	}
	removed, err := m.PruneBackups(context.Background(), "p")
	if err != nil || len(removed) != 1 || removed[0] != "backup" {
		t.Fatalf("backup pruning = %v, %v", removed, err)
	}
}

func TestCancelledDeleteLeavesResourcesAndAuditUntouched(t *testing.T) {
	m := New(nil)
	if err := m.Put(context.Background(), Resource{ID: "run", ProjectID: "p", Kind: Run, Bytes: 1}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := m.Delete(ctx, "p", []string{"run"}, "cancelled"); !errors.Is(err, context.Canceled) {
		t.Fatalf("delete error = %v", err)
	}
	if m.Usage("p") != 1 || len(m.Audit("p")) != 0 {
		t.Fatal("cancelled delete changed state")
	}
}
