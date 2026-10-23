package storage

import (
	"context"
	"database/sql"
	"errors"
	"testing"
)

func TestRunRepositoryFailsClosedWithoutProject(t *testing.T) {
	db := &fakeDB{}
	repo := NewRunRepository(db)
	if err := repo.Create(context.Background(), "", "run-a", "running"); !errors.Is(err, ErrInvalidProjectScope) {
		t.Fatalf("Create error = %v, want ErrInvalidProjectScope", err)
	}
	if db.lastQuery != "" {
		t.Fatal("database was called without a project scope")
	}
}

func TestRunRepositoryRejectsMissingRunID(t *testing.T) {
	db := &fakeDB{}
	repo := NewRunRepository(db)
	if err := repo.Create(context.Background(), "project-a", "", "running"); err == nil {
		t.Fatal("Create accepted missing run ID")
	}
	if db.lastQuery != "" {
		t.Fatal("database was called with missing run ID")
	}
}

type fakeDB struct {
	lastQuery string
	lastArgs  []any
	result    sql.Result
}

func (f *fakeDB) ExecContext(_ context.Context, query string, args ...any) (sql.Result, error) {
	f.lastQuery, f.lastArgs = query, args
	return f.result, nil
}
func (f *fakeDB) QueryContext(context.Context, string, ...any) (*sql.Rows, error) {
	panic("not used")
}
func (f *fakeDB) QueryRowContext(context.Context, string, ...any) *sql.Row {
	panic("not used")
}
func (f *fakeDB) BeginTx(context.Context, *sql.TxOptions) (*sql.Tx, error) {
	panic("not used")
}

type fakeResult int64

func (r fakeResult) LastInsertId() (int64, error) { return 0, nil }
func (r fakeResult) RowsAffected() (int64, error) { return int64(r), nil }
