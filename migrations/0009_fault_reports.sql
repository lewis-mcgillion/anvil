BEGIN;

CREATE TABLE fault_reports (
    id uuid PRIMARY KEY CHECK (is_uuidv7(id)),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id uuid NOT NULL,
    report jsonb NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    report_digest char(64) NOT NULL CHECK (report_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id) ON DELETE CASCADE,
    UNIQUE (project_id, run_id, report_digest)
);

CREATE INDEX fault_reports_by_run ON fault_reports (project_id, run_id, created_at);

ALTER TABLE fault_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE fault_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY fault_reports_project_isolation ON fault_reports
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0009_fault_reports');
COMMIT;
