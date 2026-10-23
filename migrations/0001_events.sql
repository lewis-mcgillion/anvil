-- Immutable event storage. The runs and projects tables are owned by the
-- preceding persistence migration; this migration deliberately does not
-- duplicate them.

CREATE TABLE IF NOT EXISTS run_streams (
    stream_id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    run_id UUID NOT NULL,
    next_sequence BIGINT NOT NULL DEFAULT 0 CHECK (next_sequence >= 0),
    UNIQUE (project_id, stream_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    run_id UUID NOT NULL,
    stream_id UUID NOT NULL REFERENCES run_streams(stream_id),
    sequence BIGINT NOT NULL CHECK (sequence >= 0),
    schema_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    trace_id TEXT,
    span_id TEXT,
    parent_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    invocation_id UUID,
    payload JSONB NOT NULL,
    content_hash BYTEA NOT NULL CHECK (octet_length(content_hash) = 32),
    UNIQUE (stream_id, sequence),
    UNIQUE (project_id, event_id)
);

CREATE OR REPLACE FUNCTION reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS events_immutable ON events;
CREATE TRIGGER events_immutable
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
