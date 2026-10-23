// Package jobs provides the durable PostgreSQL job lease protocol.
package jobs

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/lewis-mcgillion/anvil/internal/storage"
)

var (
	ErrInvalidInput = errors.New("jobs: invalid input")
	ErrNotFound     = errors.New("jobs: not found")
	ErrConflict     = errors.New("jobs: invalid state transition")
	ErrLeaseLost    = errors.New("jobs: lease lost")
)

type Job struct {
	ID             string
	ProjectID      string
	Kind           string
	Status         string
	Outcome        string
	Attempts       int
	MaxAttempts    int
	LeaseToken     string
	LeaseExpiresAt sql.NullTime
	ResultURL      sql.NullString
	Error          sql.NullString
	CreatedAt      time.Time
	FinishedAt     sql.NullTime
}

type Queue struct {
	db            storage.DB
	leaseDuration time.Duration
}

func NewQueue(db storage.DB, leaseDuration time.Duration) (*Queue, error) {
	if db == nil || leaseDuration <= 0 {
		return nil, ErrInvalidInput
	}
	return &Queue{db: db, leaseDuration: leaseDuration}, nil
}

func (q *Queue) Create(ctx context.Context, projectID, id, kind string, maxAttempts int) error {
	if projectID == "" || id == "" || kind == "" || maxAttempts <= 0 {
		return ErrInvalidInput
	}
	return storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO jobs (id, project_id, kind, max_attempts)
			VALUES ($1, $2, $3, $4)`, id, projectID, kind, maxAttempts)
		return err
	})
}

// Acquire atomically claims the oldest available job. SKIP LOCKED ensures one
// slow worker cannot prevent other workers from making progress.
func (q *Queue) Acquire(ctx context.Context, projectID, leaseToken string) (Job, error) {
	if projectID == "" || leaseToken == "" {
		return Job{}, ErrInvalidInput
	}
	var job Job
	err := storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		row := tx.QueryRowContext(ctx, `
			UPDATE jobs
			SET status = 'running',
			    outcome = CASE WHEN status IN ('running', 'cancelling') THEN 'interrupted' ELSE outcome END,
			    attempts = attempts + 1,
			    lease_token = $2, leased_at = clock_timestamp(),
			    heartbeat_at = clock_timestamp(),
			    lease_expires_at = clock_timestamp() + $3::interval,
			    error = NULL, finished_at = NULL
			WHERE id = (
				SELECT id FROM jobs
				WHERE project_id = $1
				  AND attempts < max_attempts
				  AND (
				    status = 'queued'
				    OR (status IN ('running', 'cancelling')
				        AND lease_expires_at IS NOT NULL
				        AND lease_expires_at < clock_timestamp())
				  )
				ORDER BY created_at, id
				FOR UPDATE SKIP LOCKED
				LIMIT 1
			)
			RETURNING id, project_id, kind, status, outcome, attempts,
			          max_attempts, COALESCE(lease_token::text, ''), lease_expires_at,
			          result_url, error, created_at, finished_at`,
			projectID, leaseToken, intervalLiteral(q.leaseDuration))
		return scanJob(row, &job)
	})
	if errors.Is(err, sql.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	return job, err
}

func (q *Queue) Heartbeat(ctx context.Context, projectID, jobID, leaseToken string) error {
	if projectID == "" || jobID == "" || leaseToken == "" {
		return ErrInvalidInput
	}
	return storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, `
			UPDATE jobs
			SET heartbeat_at = clock_timestamp(),
			    lease_expires_at = clock_timestamp() + $4::interval
			WHERE project_id = $1 AND id = $2 AND lease_token = $3
			  AND status IN ('running', 'cancelling')
			  AND lease_expires_at > clock_timestamp()`,
			projectID, jobID, leaseToken, intervalLiteral(q.leaseDuration))
		if err != nil {
			return err
		}
		if count, err := result.RowsAffected(); err != nil {
			return err
		} else if count == 0 {
			return ErrLeaseLost
		}
		return nil
	})
}

func (q *Queue) Complete(ctx context.Context, projectID, jobID, leaseToken, status, resultURL, detail string) error {
	if projectID == "" || jobID == "" || leaseToken == "" {
		return ErrInvalidInput
	}
	if status != "succeeded" && status != "failed" && status != "cancelled" {
		return ErrInvalidInput
	}
	outcome := status
	return storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, `
			UPDATE jobs
			SET status = $4, outcome = $4, result_url = NULLIF($5, ''),
			    error = NULLIF($6, ''), finished_at = clock_timestamp(),
			    lease_token = NULL, leased_at = NULL, heartbeat_at = NULL,
			    lease_expires_at = NULL
			WHERE project_id = $1 AND id = $2 AND lease_token = $3
			  AND status IN ('running', 'cancelling')`,
			projectID, jobID, leaseToken, outcome, resultURL, detail)
		if err != nil {
			return err
		}
		return affectedOr(ErrLeaseLost, result)
	})
}

// Cancel is idempotent for queued, cancelling, and cancelled jobs. A running
// job remains leased until the supervisor observes cancellation and completes it.
func (q *Queue) Cancel(ctx context.Context, projectID, jobID string) (Job, error) {
	if projectID == "" || jobID == "" {
		return Job{}, ErrInvalidInput
	}
	var job Job
	err := storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		row := tx.QueryRowContext(ctx, `
			UPDATE jobs
			SET status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancelling' END,
			    outcome = CASE WHEN status = 'queued' THEN 'cancelled' ELSE outcome END,
			    finished_at = CASE WHEN status = 'queued' THEN clock_timestamp() ELSE finished_at END
			WHERE project_id = $1 AND id = $2
			  AND status IN ('queued', 'running', 'cancelling', 'cancelled')
			RETURNING id, project_id, kind, status, outcome, attempts,
			          max_attempts, COALESCE(lease_token::text, ''), lease_expires_at,
			          result_url, error, created_at, finished_at`,
			projectID, jobID)
		return scanJob(row, &job)
	})
	if errors.Is(err, sql.ErrNoRows) {
		return Job{}, ErrConflict
	}
	return job, err
}

func (q *Queue) Get(ctx context.Context, projectID, jobID string) (Job, error) {
	if projectID == "" || jobID == "" {
		return Job{}, ErrInvalidInput
	}
	var job Job
	err := storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		return scanJob(tx.QueryRowContext(ctx, `
			SELECT id, project_id, kind, status, outcome, attempts, max_attempts,
			       COALESCE(lease_token::text, ''), lease_expires_at, result_url, error, created_at, finished_at
			FROM jobs WHERE project_id = $1 AND id = $2`, projectID, jobID), &job)
	})
	if errors.Is(err, sql.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	return job, err
}

// RecoverExpired requeues expired work, recording an interrupted outcome.
// Jobs which exhausted their attempts are failed explicitly instead.
func (q *Queue) RecoverExpired(ctx context.Context, projectID string) error {
	if projectID == "" {
		return ErrInvalidInput
	}
	return storage.WithProject(ctx, q.db, projectID, func(ctx context.Context, tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			UPDATE jobs
			SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
			    outcome = CASE WHEN attempts < max_attempts THEN 'interrupted' ELSE 'failed' END,
			    error = 'worker lease expired',
			    finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE clock_timestamp() END,
			    lease_token = NULL, leased_at = NULL, heartbeat_at = NULL,
			    lease_expires_at = NULL
			WHERE project_id = $1 AND status IN ('running', 'cancelling')
			  AND lease_expires_at IS NOT NULL AND lease_expires_at < clock_timestamp()`,
			projectID)
		return err
	})
}

func intervalLiteral(d time.Duration) string {
	return fmt.Sprintf("%d microseconds", d.Microseconds())
}

func scanJob(row interface{ Scan(...any) error }, job *Job) error {
	return row.Scan(&job.ID, &job.ProjectID, &job.Kind, &job.Status, &job.Outcome,
		&job.Attempts, &job.MaxAttempts, &job.LeaseToken, &job.LeaseExpiresAt,
		&job.ResultURL, &job.Error, &job.CreatedAt, &job.FinishedAt)
}

func affectedOr(err error, result sql.Result) error {
	count, rowsErr := result.RowsAffected()
	if rowsErr != nil {
		return rowsErr
	}
	if count == 0 {
		return err
	}
	return nil
}
