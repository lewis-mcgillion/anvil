-- Copyright 2026 Anvil contributors
-- SPDX-License-Identifier: Apache-2.0

BEGIN;

CREATE OR REPLACE FUNCTION bootstrap_principal(
    principal_id uuid,
    principal_organization_id uuid,
    principal_subject text,
    principal_kind text
)
RETURNS uuid
LANGUAGE sql
AS $$
    INSERT INTO principals (id, organization_id, subject, kind)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (organization_id, kind, subject) DO UPDATE
        SET subject = EXCLUDED.subject
    RETURNING id
$$;

CREATE TABLE api_keys (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
    key_prefix text NOT NULL CHECK (key_prefix ~ '^anv_[A-Za-z0-9_-]{8,32}$'),
    key_hash bytea NOT NULL CHECK (octet_length(key_hash) = 32),
    scopes text[] NOT NULL CHECK (cardinality(scopes) > 0),
    expires_at timestamptz,
    revoked_at timestamptz,
    rotated_from uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, project_id),
    UNIQUE (project_id, name),
    FOREIGN KEY (project_id, organization_id)
        REFERENCES projects(id, organization_id) ON DELETE CASCADE,
    FOREIGN KEY (principal_id, organization_id)
        REFERENCES principals(id, organization_id) ON DELETE RESTRICT,
    FOREIGN KEY (rotated_from, project_id)
        REFERENCES api_keys(id, project_id) ON DELETE RESTRICT,
    CHECK (scopes <@ ARRAY[
        'capture:write', 'runs:read', 'replay:write', 'suites:write',
        'suites:read', 'admin', 'metrics:read'
    ]::text[]),
    CHECK (
        NOT ('capture:write' = ANY(scopes) AND
             ('runs:read' = ANY(scopes) OR 'replay:write' = ANY(scopes)))
    ),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    CHECK (rotated_from IS NULL OR rotated_from <> id),
    CHECK (is_uuidv7(id))
);

CREATE INDEX api_keys_lookup_idx ON api_keys (key_hash)
    WHERE revoked_at IS NULL;

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY api_keys_project_isolation ON api_keys
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0002_authorization');

COMMIT;
