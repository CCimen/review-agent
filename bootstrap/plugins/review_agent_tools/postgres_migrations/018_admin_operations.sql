CREATE TABLE review_agent.worker_instances (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('review', 'publisher', 'webhook')),
    lease_owner TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    state TEXT NOT NULL CHECK (state IN ('running', 'draining', 'stopped'))
);
CREATE INDEX worker_instances_seen_idx ON review_agent.worker_instances (last_seen_at);

CREATE TABLE review_agent.worker_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    worker_id UUID NOT NULL REFERENCES review_agent.worker_instances(id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    event TEXT NOT NULL CHECK (event IN ('started', 'draining', 'stopped', 'review_started', 'review_returned', 'usage_unavailable')),
    review_run_id BIGINT REFERENCES review_agent.review_runs(id),
    job_id BIGINT REFERENCES review_agent.review_jobs(id)
);
CREATE INDEX worker_events_worker_idx ON review_agent.worker_events (worker_id, id DESC);

CREATE TABLE review_agent.review_attempt_usage (
    job_id BIGINT NOT NULL REFERENCES review_agent.review_jobs(id),
    lease_generation BIGINT NOT NULL CHECK (lease_generation > 0),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    prompt_tokens BIGINT NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens BIGINT NOT NULL CHECK (completion_tokens >= 0),
    total_tokens BIGINT NOT NULL CHECK (total_tokens > 0),
    PRIMARY KEY (job_id, lease_generation),
    CHECK (total_tokens = prompt_tokens + completion_tokens)
);
CREATE INDEX review_attempt_usage_recorded_idx ON review_agent.review_attempt_usage (recorded_at);
