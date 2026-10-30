BEGIN;

CREATE TABLE seal_requests (
    job_id uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id uuid NOT NULL,
    acknowledge_incomplete boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (job_id, project_id),
    UNIQUE (project_id, run_id),
    FOREIGN KEY (job_id, project_id) REFERENCES jobs(id, project_id),
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id)
);

ALTER TABLE seal_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE seal_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY seal_requests_project_isolation ON seal_requests
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0004_seal_jobs');

COMMIT;
