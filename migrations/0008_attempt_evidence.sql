BEGIN;

CREATE TABLE attempt_evidence (
    id uuid PRIMARY KEY CHECK (is_uuidv7(id)),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id uuid NOT NULL,
    evidence jsonb NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (job_id, project_id) REFERENCES jobs (id, project_id),
    UNIQUE (job_id, digest)
);

CREATE INDEX attempt_evidence_by_job ON attempt_evidence (project_id, job_id, created_at);

ALTER TABLE attempt_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempt_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY attempt_evidence_project_isolation ON attempt_evidence
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0008_attempt_evidence');
COMMIT;
