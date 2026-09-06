CREATE TABLE review_agent.finding_group_changes (
    review_run_id BIGINT NOT NULL,
    pull_request_id BIGINT NOT NULL,
    finding_id BIGINT NOT NULL,
    previous_canonical_finding_id BIGINT NOT NULL,
    canonical_finding_id BIGINT NOT NULL,
    latest_decision_id BIGINT REFERENCES review_agent.finding_decisions(id),
    evidence TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (review_run_id, finding_id),
    UNIQUE (review_run_id, pull_request_id, finding_id, canonical_finding_id),
    FOREIGN KEY (review_run_id, pull_request_id)
        REFERENCES review_agent.review_runs(id, pull_request_id),
    FOREIGN KEY (pull_request_id, finding_id)
        REFERENCES review_agent.pull_request_finding_references(pull_request_id, finding_id),
    FOREIGN KEY (pull_request_id, previous_canonical_finding_id)
        REFERENCES review_agent.pull_request_finding_references(pull_request_id, finding_id),
    FOREIGN KEY (pull_request_id, canonical_finding_id)
        REFERENCES review_agent.pull_request_finding_references(pull_request_id, finding_id),
    CHECK (btrim(evidence) <> '' AND char_length(evidence) <= 600)
);

-- The projection changes only when the associated publication becomes posted.
-- Self mappings preserve an explicit split without rewriting earlier evidence.
CREATE TABLE review_agent.pull_request_finding_groups (
    pull_request_id BIGINT NOT NULL,
    finding_id BIGINT NOT NULL,
    canonical_finding_id BIGINT NOT NULL,
    review_run_id BIGINT NOT NULL,
    PRIMARY KEY (pull_request_id, finding_id),
    FOREIGN KEY (review_run_id, pull_request_id, finding_id, canonical_finding_id)
        REFERENCES review_agent.finding_group_changes(
            review_run_id, pull_request_id, finding_id, canonical_finding_id
        )
);
CREATE INDEX pull_request_finding_groups_canonical_idx
    ON review_agent.pull_request_finding_groups(pull_request_id, canonical_finding_id);

ALTER TABLE review_agent.publication_findings
    DROP CONSTRAINT publication_findings_outcome_ck,
    ADD CONSTRAINT publication_findings_outcome_ck CHECK (
        outcome IN ('current', 'resolved', 'invalidated', 'suppressed', 'not_checked', 'reconciled')
        AND (
            (outcome = 'current' AND publication_review_run_id = source_review_run_id
             AND outcome_evidence IS NULL)
            OR (outcome <> 'current' AND outcome_evidence IS NOT NULL
                AND btrim(outcome_evidence) <> '')
        )
    );
