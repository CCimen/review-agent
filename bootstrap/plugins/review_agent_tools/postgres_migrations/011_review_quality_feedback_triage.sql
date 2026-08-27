ALTER TABLE review_agent.review_quality_feedback
    ADD CONSTRAINT review_quality_feedback_id_category_uk UNIQUE (id, category);

CREATE TABLE review_agent.review_quality_feedback_triage (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feedback_id BIGINT NOT NULL,
    feedback_category TEXT NOT NULL,
    status TEXT NOT NULL,
    stable_key TEXT,
    target_owner TEXT,
    evidence_reference TEXT,
    path TEXT,
    category TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT review_quality_feedback_triage_feedback_fk
        FOREIGN KEY (feedback_id, feedback_category)
        REFERENCES review_agent.review_quality_feedback(id, category),
    CONSTRAINT review_quality_feedback_triage_feedback_category_ck
        CHECK (feedback_category = 'missed_issue'),
    CONSTRAINT review_quality_feedback_triage_status_ck
        CHECK (
            status IN (
                'pending', 'actionable', 'duplicate', 'insufficient', 'resolved'
            )
        ),
    CONSTRAINT review_quality_feedback_triage_actionable_ck
        CHECK (
            (
                status = 'actionable'
                AND stable_key IS NOT NULL
                AND stable_key ~ '^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$'
                AND target_owner IS NOT NULL
                AND target_owner IN (
                    'source_tool', 'coverage', 'review_rule', 'profile',
                    'repository_decision', 'documentation'
                )
            )
            OR (
                status <> 'actionable'
                AND stable_key IS NULL
                AND target_owner IS NULL
            )
        ),
    CONSTRAINT review_quality_feedback_triage_optional_text_ck
        CHECK (
            (evidence_reference IS NULL OR (
                btrim(evidence_reference) <> ''
                AND char_length(evidence_reference) <= 500
            ))
            AND (path IS NULL OR (
                btrim(path) <> '' AND char_length(path) <= 500
            ))
            AND (category IS NULL OR (
                category ~ '^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$'
                AND char_length(category) <= 80
            ))
        ),
    CONSTRAINT review_quality_feedback_triage_audit_ck
        CHECK (
            btrim(actor) <> '' AND char_length(actor) <= 200
            AND btrim(reason) <> '' AND char_length(reason) <= 2000
        )
);

CREATE INDEX review_quality_feedback_triage_feedback_latest_idx
    ON review_agent.review_quality_feedback_triage (feedback_id, id DESC);

CREATE INDEX review_quality_feedback_triage_actionable_group_idx
    ON review_agent.review_quality_feedback_triage (stable_key, id)
    WHERE status = 'actionable';
