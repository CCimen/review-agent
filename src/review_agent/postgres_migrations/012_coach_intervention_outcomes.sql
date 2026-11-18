CREATE TABLE review_agent.coach_intervention_outcomes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    coach_candidate_id BIGINT NOT NULL,
    intervention_key TEXT NOT NULL,
    proposal_content_hash TEXT NOT NULL,
    base_contract_hash TEXT NOT NULL,
    diff_hash TEXT,
    validation_receipt_hash TEXT,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT coach_intervention_outcomes_candidate_fk
        FOREIGN KEY (coach_candidate_id)
        REFERENCES review_agent.coach_candidates(id),
    CONSTRAINT coach_intervention_outcomes_key_uk UNIQUE (intervention_key),
    CONSTRAINT coach_intervention_outcomes_hashes_ck
        CHECK (
            intervention_key ~ '^sha256:[0-9a-f]{64}$'
            AND proposal_content_hash ~ '^sha256:[0-9a-f]{64}$'
            AND base_contract_hash ~ '^sha256:[0-9a-f]{64}$'
            AND (
                diff_hash IS NULL
                OR diff_hash ~ '^sha256:[0-9a-f]{64}$'
            )
            AND (
                validation_receipt_hash IS NULL
                OR validation_receipt_hash ~ '^sha256:[0-9a-f]{64}$'
            )
        ),
    CONSTRAINT coach_intervention_outcomes_outcome_ck
        CHECK (
            outcome IN (
                'accepted',
                'rejected_regression',
                'rejected_no_improvement',
                'rejected_insufficient_evidence',
                'rejected_wrong_owner',
                'withdrawn'
            )
        ),
    CONSTRAINT coach_intervention_outcomes_evaluation_ck
        CHECK (
            outcome NOT IN (
                'accepted',
                'rejected_regression',
                'rejected_no_improvement'
            )
            OR (diff_hash IS NOT NULL AND validation_receipt_hash IS NOT NULL)
        ),
    CONSTRAINT coach_intervention_outcomes_audit_ck
        CHECK (
            btrim(reason) <> '' AND char_length(reason) <= 2000
            AND btrim(actor) <> '' AND char_length(actor) <= 200
        )
);

CREATE INDEX coach_intervention_outcomes_candidate_latest_idx
    ON review_agent.coach_intervention_outcomes (
        coach_candidate_id, recorded_at DESC, id DESC
    );

CREATE INDEX coach_runs_proposal_repository_idx
    ON review_agent.coach_runs (proposal_set_id, repository_id, id);
