CREATE TABLE review_agent.review_decision_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    base_sha TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    index_path TEXT NOT NULL,
    index_hash TEXT,
    snapshot_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    matched_decision_count INTEGER NOT NULL,
    failure_code TEXT,
    loaded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT review_decision_snapshots_run_fk
        FOREIGN KEY (review_run_id) REFERENCES review_agent.review_runs(id),
    CONSTRAINT review_decision_snapshots_run_uk
        UNIQUE (review_run_id),
    CONSTRAINT review_decision_snapshots_schema_ck
        CHECK (schema_version = 1),
    CONSTRAINT review_decision_snapshots_status_ck
        CHECK (
            status IN (
                'not_configured', 'loaded', 'unavailable', 'invalid',
                'too_many_matches'
            )
        ),
    CONSTRAINT review_decision_snapshots_path_ck
        CHECK (index_path = '.review-agent/decisions.toml'),
    CONSTRAINT review_decision_snapshots_hashes_ck
        CHECK (
            base_sha ~ '^[0-9a-f]{40,64}$'
            AND (index_hash IS NULL OR index_hash ~ '^sha256:[0-9a-f]{64}$')
            AND snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    CONSTRAINT review_decision_snapshots_payload_ck
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 524288
        ),
    CONSTRAINT review_decision_snapshots_count_ck
        CHECK (matched_decision_count BETWEEN 0 AND 10),
    CONSTRAINT review_decision_snapshots_lifecycle_ck
        CHECK (
            (
                status = 'loaded'
                AND index_hash IS NOT NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'not_configured'
                AND index_hash IS NULL
                AND matched_decision_count = 0
                AND failure_code IS NULL
            )
            OR (
                status IN ('unavailable', 'invalid', 'too_many_matches')
                AND matched_decision_count = 0
                AND failure_code ~ '^[a-z][a-z0-9_]{0,79}$'
            )
        )
);

COMMENT ON TABLE review_agent.review_decision_snapshots IS
    'One immutable, versioned ADR evidence aggregate read from the exact base of a review run';
