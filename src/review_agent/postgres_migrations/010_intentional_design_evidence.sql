ALTER TABLE review_agent.review_decision_snapshots
    ADD CONSTRAINT review_decision_snapshots_run_identity_uk
    UNIQUE (id, review_run_id, base_sha);

ALTER TABLE review_agent.finding_decisions
    ADD CONSTRAINT finding_decisions_occurrence_kind_uk
    UNIQUE (id, finding_occurrence_id, decision);

CREATE TABLE review_agent.intentional_design_evidence (
    finding_decision_id BIGINT PRIMARY KEY,
    finding_occurrence_id BIGINT NOT NULL,
    review_run_id BIGINT NOT NULL,
    decision_kind TEXT NOT NULL,
    review_decision_snapshot_id BIGINT NOT NULL,
    repository_decision_id TEXT NOT NULL,
    repository_decision_metadata_hash TEXT NOT NULL,
    repository_decision_path TEXT NOT NULL,
    repository_decision_base_sha TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT intentional_design_evidence_decision_fk
        FOREIGN KEY (
            finding_decision_id, finding_occurrence_id, decision_kind
        ) REFERENCES review_agent.finding_decisions (
            id, finding_occurrence_id, decision
        ),
    CONSTRAINT intentional_design_evidence_occurrence_run_fk
        FOREIGN KEY (finding_occurrence_id, review_run_id)
        REFERENCES review_agent.finding_occurrences (id, review_run_id),
    CONSTRAINT intentional_design_evidence_snapshot_run_fk
        FOREIGN KEY (
            review_decision_snapshot_id, review_run_id,
            repository_decision_base_sha
        ) REFERENCES review_agent.review_decision_snapshots (
            id, review_run_id, base_sha
        ),
    CONSTRAINT intentional_design_evidence_kind_ck
        CHECK (decision_kind = 'intentional_by_design'),
    CONSTRAINT intentional_design_evidence_id_ck
        CHECK (
            repository_decision_id
                ~ '^ADR-[0-9A-Za-z][0-9A-Za-z._-]{0,63}$'
        ),
    CONSTRAINT intentional_design_evidence_hash_ck
        CHECK (
            repository_decision_metadata_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    CONSTRAINT intentional_design_evidence_path_ck
        CHECK (
            char_length(repository_decision_path) BETWEEN 1 AND 500
            AND repository_decision_path !~ '(^|/)\.\.?(/|$)'
            AND left(repository_decision_path, 1) <> '/'
            AND position(chr(92) in repository_decision_path) = 0
        ),
    CONSTRAINT intentional_design_evidence_base_sha_ck
        CHECK (repository_decision_base_sha ~ '^[0-9a-f]{40,64}$')
);

COMMENT ON TABLE review_agent.intentional_design_evidence IS
    'Exact accepted ADR snapshot provenance for one intentional-by-design finding decision';
