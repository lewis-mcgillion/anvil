BEGIN;

-- A comparison is the durable record of "we asked whether these two runs
-- agree". It is created queued, resolved asynchronously by a job, and read
-- back many times, so the three statuses are stored as independent columns
-- rather than derived from a blob: a consumer must be able to see what each
-- layer said without the server re-deriving it.
CREATE TABLE comparisons (
    id uuid PRIMARY KEY CHECK (is_uuidv7(id)),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    baseline_recording_id uuid NOT NULL,
    candidate_run_id uuid NOT NULL,
    job_id uuid NOT NULL,
    -- Mirrors the published JobStatus enum.
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'cancelling', 'cancelled',
                          'succeeded', 'failed')),
    -- The three independent statuses. They start at their "no evidence yet"
    -- values, which are deliberately not passes: a comparison that never ran
    -- must never read as having passed.
    deterministic text NOT NULL DEFAULT 'inconclusive'
        CHECK (deterministic IN ('pass', 'fail', 'inconclusive')),
    judge text NOT NULL DEFAULT 'not_run'
        CHECK (judge IN ('pass', 'fail', 'inconclusive', 'not_run')),
    infrastructure text NOT NULL DEFAULT 'ok'
        CHECK (infrastructure IN ('ok', 'error', 'cancelled', 'timed_out')),
    alignment_confidence text NOT NULL DEFAULT 'partial'
        CHECK (alignment_confidence IN ('exact', 'partial', 'ambiguous')),
    judge_profile text CHECK (judge_profile IS NULL
        OR char_length(judge_profile) BETWEEN 1 AND 64),
    -- The versioned report. Null until the job produces one.
    report_version text CHECK (report_version IS NULL
        OR char_length(report_version) BETWEEN 1 AND 32),
    report jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (id, project_id)
);

CREATE INDEX comparisons_project_created ON comparisons (project_id, created_at DESC, id DESC);

ALTER TABLE comparisons ENABLE ROW LEVEL SECURITY;
ALTER TABLE comparisons FORCE ROW LEVEL SECURITY;
CREATE POLICY comparisons_project_isolation ON comparisons
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

-- Findings are stored as rows rather than inside the report blob because they
-- are the paginated resource. `ordinal` is the detector's deterministic order,
-- and paginating on it keeps a page stable even though findings carry no
-- natural time ordering of their own.
CREATE TABLE comparison_divergences (
    id uuid PRIMARY KEY CHECK (is_uuidv7(id)),
    comparison_id uuid NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    category text NOT NULL CHECK (category IN (
        'changed_input', 'changed_prompt', 'changed_tool_name', 'changed_tool_args',
        'missing_tool_call', 'extra_tool_call', 'control_flow', 'changed_output',
        'error_difference', 'budget_exceeded', 'capture_gap', 'changed_model',
        'ambiguous_alignment')),
    summary text NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 2048),
    evidence_event_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (comparison_id, project_id) REFERENCES comparisons (id, project_id) ON DELETE CASCADE,
    UNIQUE (comparison_id, ordinal)
);

CREATE INDEX comparison_divergences_page
    ON comparison_divergences (project_id, comparison_id, ordinal);

ALTER TABLE comparison_divergences ENABLE ROW LEVEL SECURITY;
ALTER TABLE comparison_divergences FORCE ROW LEVEL SECURITY;
CREATE POLICY comparison_divergences_project_isolation ON comparison_divergences
    USING (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid)
    WITH CHECK (project_id = nullif(current_setting('anvil.project_id', true), '')::uuid);

INSERT INTO schema_migrations (version) VALUES ('0007_comparisons');

COMMIT;
