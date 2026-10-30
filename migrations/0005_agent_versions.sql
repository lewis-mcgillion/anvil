BEGIN;

CREATE TABLE agent_versions (
    id uuid PRIMARY KEY CHECK (is_uuidv7(id)),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    image text NOT NULL CHECK (image ~ '^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$'),
    entrypoint text NOT NULL CHECK (char_length(entrypoint) BETWEEN 1 AND 512),
    adapter text NOT NULL CHECK (char_length(adapter) BETWEEN 1 AND 64),
    adapter_version text NOT NULL CHECK (char_length(adapter_version) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, project_id),
    UNIQUE (project_id, image, entrypoint, adapter, adapter_version)
);

ALTER TABLE agent_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_versions_project_isolation ON agent_versions
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0005_agent_versions');

COMMIT;
