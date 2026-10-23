-- Copyright 2026 Anvil contributors
-- SPDX-License-Identifier: Apache-2.0

BEGIN;

ALTER TABLE projects
    ADD COLUMN retention_days integer NOT NULL DEFAULT 30
        CHECK (retention_days BETWEEN 1 AND 365);

CREATE TABLE idempotency_keys (
    project_id uuid NOT NULL,
    key text NOT NULL CHECK (key ~ '^[A-Za-z0-9_-]{8,128}$'),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status_code integer NOT NULL,
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE deletion_requests (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    target_kind text NOT NULL CHECK (target_kind IN ('run', 'recording', 'suite')),
    target_id uuid NOT NULL,
    job_id uuid NOT NULL,
    audit_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, project_id),
    UNIQUE (project_id, target_kind, target_id),
    CHECK (is_uuidv7(id)),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (job_id, project_id) REFERENCES jobs(id, project_id)
);

CREATE TABLE audit_events (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    kind text NOT NULL,
    root_ids jsonb NOT NULL CHECK (jsonb_typeof(root_ids) = 'array'),
    resource_ids jsonb NOT NULL CHECK (jsonb_typeof(resource_ids) = 'array'),
    bytes bigint NOT NULL CHECK (bytes >= 0),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (is_uuidv7(id)),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT
);

ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY idempotency_keys_project_isolation ON idempotency_keys
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

ALTER TABLE deletion_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY deletion_requests_project_isolation ON deletion_requests
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_events_project_isolation ON audit_events
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0002_administration');

COMMIT;
