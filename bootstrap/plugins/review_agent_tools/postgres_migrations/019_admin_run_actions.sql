CREATE TABLE review_agent.admin_run_actions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL REFERENCES review_agent.review_runs(id),
    job_id BIGINT NOT NULL REFERENCES review_agent.review_jobs(id),
    action TEXT NOT NULL CHECK (action IN ('release_retry', 'cancel', 'mark_stalled')),
    actor TEXT NOT NULL CHECK (btrim(actor) <> '' AND length(actor) <= 200),
    reason TEXT NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 500),
    previous_run_status TEXT NOT NULL CHECK (
        previous_run_status IN ('running', 'completed', 'failed', 'superseded')
    ),
    previous_job_status TEXT NOT NULL CHECK (
        previous_job_status IN (
            'queued', 'leased', 'awaiting_publication', 'superseded',
            'succeeded', 'failed', 'dead_letter'
        )
    ),
    expected_lease_generation BIGINT NOT NULL CHECK (expected_lease_generation >= 0),
    expected_available_at TIMESTAMPTZ NOT NULL,
    stale_after_minutes INTEGER,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT admin_run_actions_stale_ck CHECK (
        (action = 'mark_stalled' AND stale_after_minutes BETWEEN 1 AND 1440)
        OR (action <> 'mark_stalled' AND stale_after_minutes IS NULL)
    )
);

CREATE INDEX admin_run_actions_run_idx
    ON review_agent.admin_run_actions (review_run_id, id DESC);
