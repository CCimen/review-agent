-- Existing records retain their identities and their code-review behavior.
ALTER TABLE review_agent.review_subjects
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT review_subjects_purpose_ck CHECK (purpose IN ('code', 'documentation')),
    ADD CONSTRAINT review_subjects_purpose_identity_uk UNIQUE (id, purpose),
    DROP CONSTRAINT review_subjects_identity_uk,
    ADD CONSTRAINT review_subjects_identity_uk UNIQUE (
        pull_request_id, purpose, base_sha, head_sha, policy_revision,
        resolved_config_schema_version, resolved_config_hash
    );

ALTER TABLE review_agent.review_runs
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT review_runs_subject_purpose_fk
        FOREIGN KEY (review_subject_id, purpose)
        REFERENCES review_agent.review_subjects(id, purpose),
    ADD CONSTRAINT review_runs_purpose_identity_uk UNIQUE (id, purpose);

DROP INDEX review_agent.review_runs_active_pull_request_idx;
CREATE UNIQUE INDEX review_runs_active_pull_request_idx
    ON review_agent.review_runs (pull_request_id, purpose)
    WHERE status = 'running';
CREATE INDEX review_runs_purpose_history_idx
    ON review_agent.review_runs (pull_request_id, purpose, id DESC);

ALTER TABLE review_agent.publications
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT publications_run_purpose_fk
        FOREIGN KEY (review_run_id, purpose)
        REFERENCES review_agent.review_runs(id, purpose),
    ADD CONSTRAINT publications_purpose_identity_uk UNIQUE (id, purpose),
    ADD CONSTRAINT publications_superseded_purpose_fk
        FOREIGN KEY (superseded_by_publication_id, purpose)
        REFERENCES review_agent.publications(id, purpose);

DROP INDEX review_agent.publications_current_posted_idx;
CREATE UNIQUE INDEX publications_current_posted_idx
    ON review_agent.publications (pull_request_id, purpose)
    WHERE status = 'posted' AND superseded_by_publication_id IS NULL;

ALTER TABLE review_agent.finding_identities
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT finding_identities_purpose_ck CHECK (purpose IN ('code', 'documentation')),
    ADD CONSTRAINT finding_identities_purpose_identity_uk UNIQUE (id, purpose),
    DROP CONSTRAINT finding_identities_repository_fingerprint_uk,
    ADD CONSTRAINT finding_identities_repository_fingerprint_uk
        UNIQUE (repository_id, purpose, fingerprint);

ALTER TABLE review_agent.finding_occurrences
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT finding_occurrences_run_purpose_fk
        FOREIGN KEY (review_run_id, purpose)
        REFERENCES review_agent.review_runs(id, purpose),
    ADD CONSTRAINT finding_occurrences_identity_purpose_fk
        FOREIGN KEY (finding_id, purpose)
        REFERENCES review_agent.finding_identities(id, purpose),
    ADD CONSTRAINT finding_occurrences_purpose_identity_uk UNIQUE (id, purpose);

ALTER TABLE review_agent.publication_findings
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT publication_findings_publication_purpose_fk
        FOREIGN KEY (publication_id, purpose)
        REFERENCES review_agent.publications(id, purpose),
    ADD CONSTRAINT publication_findings_occurrence_purpose_fk
        FOREIGN KEY (source_finding_occurrence_id, purpose)
        REFERENCES review_agent.finding_occurrences(id, purpose);

-- Group projections already reference their exact immutable change record.
ALTER TABLE review_agent.finding_group_changes
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'code',
    ADD CONSTRAINT finding_group_changes_run_purpose_fk
        FOREIGN KEY (review_run_id, purpose)
        REFERENCES review_agent.review_runs(id, purpose),
    ADD CONSTRAINT finding_group_changes_member_purpose_fk
        FOREIGN KEY (finding_id, purpose)
        REFERENCES review_agent.finding_identities(id, purpose),
    ADD CONSTRAINT finding_group_changes_previous_purpose_fk
        FOREIGN KEY (previous_canonical_finding_id, purpose)
        REFERENCES review_agent.finding_identities(id, purpose),
    ADD CONSTRAINT finding_group_changes_canonical_purpose_fk
        FOREIGN KEY (canonical_finding_id, purpose)
        REFERENCES review_agent.finding_identities(id, purpose);
