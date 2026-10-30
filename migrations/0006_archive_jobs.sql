BEGIN;

ALTER TABLE recordings ADD CONSTRAINT recordings_id_project_unique UNIQUE (id, project_id);

CREATE TABLE archive_exports (
    job_id uuid NOT NULL,
    project_id uuid NOT NULL,
    recording_id uuid NOT NULL,
    include_artifacts boolean NOT NULL DEFAULT true,
    redaction_profile text,
    content bytea,
    sha256_hash char(64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (job_id, project_id),
    FOREIGN KEY (job_id, project_id) REFERENCES jobs(id, project_id),
    FOREIGN KEY (recording_id, project_id) REFERENCES recordings(id, project_id),
    CHECK (redaction_profile IS NULL OR char_length(redaction_profile) BETWEEN 1 AND 128),
    CHECK ((content IS NULL AND sha256_hash IS NULL) OR
           (content IS NOT NULL AND sha256_hash ~ '^[0-9a-f]{64}$'))
);

ALTER TABLE archive_exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_exports FORCE ROW LEVEL SECURITY;
CREATE POLICY archive_exports_project_isolation ON archive_exports
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0006_archive_jobs');

COMMIT;
