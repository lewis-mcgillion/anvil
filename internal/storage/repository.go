package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
)

// DB is implemented by *sql.DB. Keeping the repository dependent on this
// small interface makes it possible to exercise its authorization predicates
// without a database-specific mock.
type DB interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
	BeginTx(context.Context, *sql.TxOptions) (*sql.Tx, error)
}

func validateProjectID(projectID string) error {
	if projectID == "" {
		return ErrInvalidProjectScope
	}
	return nil
}

// WithProject runs fn in a transaction with a transaction-local RLS context.
// SET LOCAL is deliberately used instead of SET: a pooled connection cannot
// retain the caller's project after the transaction is committed or rolled
// back.
func WithProject(ctx context.Context, db DB, projectID string, fn func(context.Context, *sql.Tx) error) error {
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin project transaction: %w", err)
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback()
		}
	}()

	if _, err := tx.ExecContext(ctx,
		"SELECT set_config('anvil.project_id', $1, true)", projectID,
	); err != nil {
		return fmt.Errorf("set project scope: %w", err)
	}
	if err := fn(ctx, tx); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit project transaction: %w", err)
	}
	committed = true
	return nil
}

type Run struct {
	ID        string
	ProjectID string
	Status    string
}

type RunRepository struct {
	db DB
}

func NewRunRepository(db DB) *RunRepository {
	return &RunRepository{db: db}
}

func (r *RunRepository) Create(ctx context.Context, projectID, runID, status string) error {
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	if runID == "" {
		return errors.New("storage: invalid run id")
	}
	_, err := r.db.ExecContext(ctx,
		`INSERT INTO runs (id, project_id, outcome) VALUES ($1, $2, $3)`,
		runID, projectID, status,
	)
	return err
}

func (r *RunRepository) Get(ctx context.Context, projectID, runID string) (Run, error) {
	if err := validateProjectID(projectID); err != nil {
		return Run{}, err
	}
	var run Run
	err := r.db.QueryRowContext(ctx,
		`SELECT id, project_id, outcome FROM runs
		 WHERE project_id = $1 AND id = $2`,
		projectID, runID,
	).Scan(&run.ID, &run.ProjectID, &run.Status)
	if err == sql.ErrNoRows {
		return Run{}, ErrNotFound
	}
	if err != nil {
		return Run{}, err
	}
	return run, nil
}

func (r *RunRepository) UpdateStatus(ctx context.Context, projectID, runID, status string) error {
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	result, err := r.db.ExecContext(ctx,
		`UPDATE runs SET outcome = $1
		 WHERE project_id = $2 AND id = $3`,
		status, projectID, runID,
	)
	if err != nil {
		return err
	}
	return rowsAffectedOrNotFound(result)
}

func (r *RunRepository) Delete(ctx context.Context, projectID, runID string) error {
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	result, err := r.db.ExecContext(ctx,
		`DELETE FROM runs WHERE project_id = $1 AND id = $2`,
		projectID, runID,
	)
	if err != nil {
		return err
	}
	return rowsAffectedOrNotFound(result)
}

func rowsAffectedOrNotFound(result sql.Result) error {
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count == 0 {
		return ErrNotFound
	}
	return nil
}
