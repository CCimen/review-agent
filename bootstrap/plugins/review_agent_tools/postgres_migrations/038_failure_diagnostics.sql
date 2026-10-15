-- A short, code-owned or upstream-redacted cause beside a terminal run's code.
ALTER TABLE review_agent.review_runs
    ADD COLUMN failure_detail TEXT,
    ADD CONSTRAINT review_runs_failure_detail_ck CHECK (
        failure_detail IS NULL
        OR (failure_code IS NOT NULL AND char_length(failure_detail) BETWEEN 1 AND 300)
    );

-- Why one worker attempt (job lease generation) ended without finishing its
-- review. Claims reset the job's failure code, so this keeps each attempt.
CREATE TABLE review_agent.review_attempt_failures (
    job_id BIGINT NOT NULL REFERENCES review_agent.review_jobs(id),
    lease_generation BIGINT NOT NULL CHECK (lease_generation > 0),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    failure_code TEXT NOT NULL CHECK (
        btrim(failure_code) <> '' AND char_length(failure_code) <= 80
    ),
    failure_detail TEXT CHECK (
        failure_detail IS NULL OR char_length(failure_detail) BETWEEN 1 AND 300
    ),
    PRIMARY KEY (job_id, lease_generation)
);
CREATE INDEX review_attempt_failures_recorded_idx
    ON review_agent.review_attempt_failures (recorded_at);
