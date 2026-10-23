CREATE TABLE organizations (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, name)
);

CREATE TABLE principals (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    subject text NOT NULL,
    UNIQUE (organization_id, subject)
);

CREATE TABLE project_grants (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    principal_id uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    role text NOT NULL,
    PRIMARY KEY (project_id, principal_id)
);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    outcome text NOT NULL DEFAULT 'unknown',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id)
);

CREATE TABLE run_streams (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    UNIQUE (project_id, run_id, id),
    FOREIGN KEY (project_id, run_id) REFERENCES runs(project_id, id) ON DELETE CASCADE
);

CREATE TABLE events (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stream_id uuid NOT NULL REFERENCES run_streams(id) ON DELETE CASCADE,
    sequence bigint NOT NULL CHECK (sequence >= 0),
    schema_version text NOT NULL,
    type text NOT NULL,
    payload jsonb NOT NULL,
    content_hash bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    UNIQUE (project_id, stream_id, sequence),
    UNIQUE (project_id, id, content_hash),
    FOREIGN KEY (project_id, run_id) REFERENCES runs(project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, stream_id) REFERENCES run_streams(project_id, id) ON DELETE CASCADE
);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sha256 bytea NOT NULL,
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    UNIQUE (project_id, sha256),
    FOREIGN KEY (project_id, run_id) REFERENCES runs(project_id, id) ON DELETE CASCADE
);

CREATE TABLE recordings (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    manifest jsonb NOT NULL,
    content_hash bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    UNIQUE (project_id, run_id),
    FOREIGN KEY (project_id, run_id) REFERENCES runs(project_id, id) ON DELETE CASCADE
);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'project_grants', 'runs', 'run_streams', 'events',
        'artifacts', 'recordings'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I
             USING (project_id = NULLIF(current_setting(''anvil.project_id'', true), '''')::uuid)
             WITH CHECK (project_id = NULLIF(current_setting(''anvil.project_id'', true), '''')::uuid)',
            table_name || '_project_isolation', table_name
        );
    END LOOP;
END $$;
