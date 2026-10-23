package jobs

import (
	"context"
	"database/sql"
	"testing"
	"time"
)

func TestNewQueueRejectsInvalidLeaseConfiguration(t *testing.T) {
	if _, err := NewQueue(nil, time.Second); err != ErrInvalidInput {
		t.Fatalf("nil database error = %v, want ErrInvalidInput", err)
	}
	if _, err := NewQueue(&stubDB{}, 0); err != ErrInvalidInput {
		t.Fatalf("zero lease duration error = %v, want ErrInvalidInput", err)
	}
}

func TestQueueRejectsUnscopedOperationsBeforeDatabaseAccess(t *testing.T) {
	q, err := NewQueue(&stubDB{}, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := q.Create(t.Context(), "", "job", "export", 1); err != ErrInvalidInput {
		t.Fatalf("Create error = %v, want ErrInvalidInput", err)
	}
	if _, err := q.Acquire(t.Context(), "", "lease"); err != ErrInvalidInput {
		t.Fatalf("Acquire error = %v, want ErrInvalidInput", err)
	}
	if err := q.Heartbeat(t.Context(), "", "job", "lease"); err != ErrInvalidInput {
		t.Fatalf("Heartbeat error = %v, want ErrInvalidInput", err)
	}
}

func TestIntervalLiteralUsesMicroseconds(t *testing.T) {
	if got := intervalLiteral(1500 * time.Millisecond); got != "1500000 microseconds" {
		t.Fatalf("intervalLiteral() = %q", got)
	}
}

type stubDB struct{}

func (stubDB) ExecContext(context.Context, string, ...any) (sql.Result, error) {
	panic("database should not be called")
}
func (stubDB) QueryContext(context.Context, string, ...any) (*sql.Rows, error) {
	panic("database should not be called")
}
func (stubDB) QueryRowContext(context.Context, string, ...any) *sql.Row {
	panic("database should not be called")
}
func (stubDB) BeginTx(context.Context, *sql.TxOptions) (*sql.Tx, error) {
	panic("database should not be called")
}
