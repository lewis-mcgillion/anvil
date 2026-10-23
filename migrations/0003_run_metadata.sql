BEGIN;

ALTER TABLE runs ADD COLUMN agent_name text;
ALTER TABLE runs ADD COLUMN adapter text;
ALTER TABLE runs ADD COLUMN adapter_version text;
ALTER TABLE runs ADD COLUMN trace_id text;

ALTER TABLE runs ADD CONSTRAINT runs_agent_name_length
    CHECK (agent_name IS NULL OR char_length(agent_name) BETWEEN 1 AND 256);
ALTER TABLE runs ADD CONSTRAINT runs_adapter_length
    CHECK (adapter IS NULL OR char_length(adapter) BETWEEN 1 AND 64);
ALTER TABLE runs ADD CONSTRAINT runs_adapter_version_length
    CHECK (adapter_version IS NULL OR char_length(adapter_version) BETWEEN 1 AND 64);
ALTER TABLE runs ADD CONSTRAINT runs_trace_id_format
    CHECK (trace_id IS NULL OR trace_id ~ '^[0-9a-f]{32}$');

INSERT INTO schema_migrations (version) VALUES ('0003_run_metadata');

COMMIT;
