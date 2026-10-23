-- Copyright 2026 Anvil contributors
-- SPDX-License-Identifier: Apache-2.0

BEGIN;

CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN (
        'seal', 'export', 'import', 'reexecution', 'comparison',
        'suite_run', 'deletion'
    )),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'cancelling', 'cancelled', 'succeeded', 'failed')),
    outcome text NOT NULL DEFAULT 'none'
        CHECK (outcome IN ('none', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    lease_token uuid,
    leased_at timestamptz,
    heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    result_url text,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    CHECK (is_uuidv7(id)),
    CHECK (lease_token IS NULL OR is_uuidv7(lease_token)),
    CHECK (finished_at IS NULL OR finished_at >= created_at),
    CHECK (
        status IN ('queued', 'running', 'cancelling')
        OR finished_at IS NOT NULL
    ),
    UNIQUE (id, project_id)
);

CREATE INDEX jobs_polling_idx
    ON jobs (project_id, status, lease_expires_at, created_at, id)
    WHERE status IN ('queued', 'running', 'cancelling');

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY jobs_project_isolation ON jobs
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0002_jobs');

COMMIT;
