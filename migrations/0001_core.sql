-- Copyright 2026 Anvil contributors
-- SPDX-License-Identifier: Apache-2.0

BEGIN;

CREATE TABLE schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION is_uuidv7(value uuid)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT (get_byte(uuid_send(value), 6) >> 4) = 7
$$;

CREATE TABLE organizations (
    id uuid PRIMARY KEY,
    slug text NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (slug),
    CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    CHECK (is_uuidv7(id))
);

CREATE TABLE projects (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    slug text NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (organization_id, slug),
    UNIQUE (id, organization_id),
    CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    CHECK (is_uuidv7(id))
);

CREATE TABLE principals (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    subject text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('user', 'service')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (organization_id, kind, subject),
    UNIQUE (id, organization_id),
    CHECK (is_uuidv7(id))
);

CREATE TABLE project_grants (
    project_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, principal_id),
    FOREIGN KEY (project_id, organization_id)
        REFERENCES projects(id, organization_id) ON DELETE CASCADE,
    FOREIGN KEY (principal_id, organization_id)
        REFERENCES principals(id, organization_id) ON DELETE CASCADE
);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    capture_status text NOT NULL DEFAULT 'open'
        CHECK (capture_status IN ('open', 'sealing', 'sealed_complete', 'sealed_incomplete')),
    execution_outcome text NOT NULL DEFAULT 'unknown'
        CHECK (execution_outcome IN ('running', 'succeeded', 'failed', 'cancelled', 'unknown')),
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    UNIQUE (id, project_id),
    CHECK (is_uuidv7(id))
);

CREATE TABLE run_streams (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, name),
    UNIQUE (id, run_id, project_id),
    CHECK (is_uuidv7(id)),
    FOREIGN KEY (run_id, project_id)
        REFERENCES runs(id, project_id) ON DELETE CASCADE
);

CREATE TABLE events (
    event_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    stream_id uuid NOT NULL,
    project_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 0),
    schema_version text NOT NULL,
    type text NOT NULL CHECK (type IN (
        'run.started', 'run.completed', 'run.failed', 'message.created',
        'model.requested', 'model.chunk', 'model.completed', 'model.failed',
        'tool.requested', 'tool.completed', 'tool.failed',
        'state.checkpoint', 'capture.gap', 'fault.applied'
    )),
    occurred_at timestamptz NOT NULL,
    trace_id text,
    span_id text,
    parent_event_ids uuid[] NOT NULL DEFAULT '{}',
    invocation_id uuid,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    content_hash char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    replay_safe boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (stream_id, sequence),
    CHECK (is_uuidv7(event_id)),
    CHECK (invocation_id IS NULL OR is_uuidv7(invocation_id)),
    FOREIGN KEY (stream_id, run_id, project_id)
        REFERENCES run_streams(id, run_id, project_id) ON DELETE CASCADE
);

CREATE INDEX events_run_sequence_idx ON events (run_id, stream_id, sequence);
CREATE INDEX events_payload_idx ON events USING gin (payload);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    media_type text NOT NULL,
    sha256_hash char(64) NOT NULL CHECK (sha256_hash ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 8388608),
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, sha256_hash),
    CHECK (octet_length(content) = size_bytes),
    CHECK (is_uuidv7(id)),
    FOREIGN KEY (run_id, project_id)
        REFERENCES runs(id, project_id) ON DELETE CASCADE
);

CREATE TABLE recordings (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    schema_version text NOT NULL,
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    sha256_hash char(64) NOT NULL CHECK (sha256_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id),
    UNIQUE (project_id, sha256_hash),
    CHECK (is_uuidv7(id)),
    FOREIGN KEY (run_id, project_id)
        REFERENCES runs(id, project_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'immutable row cannot be changed in %', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER events_are_immutable
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

CREATE TRIGGER recordings_are_immutable
    BEFORE UPDATE OR DELETE ON recordings
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

INSERT INTO schema_migrations (version) VALUES ('0001_core');

COMMIT;
