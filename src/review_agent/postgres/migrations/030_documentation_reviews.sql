CREATE TABLE review_agent.documentation_reviews (
    review_run_id BIGINT PRIMARY KEY,
    purpose TEXT NOT NULL DEFAULT 'documentation' CHECK (purpose = 'documentation'),
    base_sha TEXT NOT NULL CHECK (base_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    comparison_sha TEXT CHECK (comparison_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    head_sha TEXT NOT NULL CHECK (head_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    scope_json JSONB CHECK (jsonb_typeof(scope_json) = 'object'),
    evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(evidence_json) = 'array' AND jsonb_array_length(evidence_json) <= 512),
    assessment_json JSONB CHECK (jsonb_typeof(assessment_json) = 'object'),
    outcome TEXT CHECK (outcome IN (
        'not_needed', 'no_mismatch_found', 'findings', 'incomplete',
        'not_configured', 'invalid_configuration', 'unavailable'
    )),
    semantic_inference_used BOOLEAN NOT NULL DEFAULT FALSE,
    incomplete_reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(incomplete_reasons_json) = 'array'
            AND jsonb_array_length(incomplete_reasons_json) <= 512),
    coverage_complete BOOLEAN NOT NULL DEFAULT FALSE,
    frozen_at TIMESTAMPTZ,
    CONSTRAINT documentation_reviews_run_purpose_fk
        FOREIGN KEY (review_run_id, purpose)
        REFERENCES review_agent.review_runs(id, purpose),
    CONSTRAINT documentation_reviews_frozen_outcome_ck
        CHECK ((frozen_at IS NULL AND outcome IS NULL)
            OR (frozen_at IS NOT NULL AND outcome IS NOT NULL AND scope_json IS NOT NULL)),
    CONSTRAINT documentation_reviews_size_ck CHECK (
        octet_length(COALESCE(scope_json::text, 'null'))
        + octet_length(evidence_json::text)
        + octet_length(incomplete_reasons_json::text)
        + octet_length(COALESCE(assessment_json::text, 'null')) <= 524288
    ),
    CONSTRAINT documentation_reviews_clean_ck CHECK (
        outcome NOT IN ('not_needed', 'no_mismatch_found') OR (
            coverage_complete AND comparison_sha IS NOT NULL
            AND incomplete_reasons_json = '[]'::jsonb
        )
    )
);
