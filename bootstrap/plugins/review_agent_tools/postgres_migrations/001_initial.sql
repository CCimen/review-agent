CREATE SCHEMA review_agent;

CREATE TABLE review_agent.repositories (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_repository_id BIGINT NOT NULL,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT repositories_provider_identity_uk
        UNIQUE (provider, provider_repository_id),
    CONSTRAINT repositories_provider_ck
        CHECK (provider ~ '^[a-z][a-z0-9_-]*$'),
    CONSTRAINT repositories_provider_repository_id_ck
        CHECK (provider_repository_id > 0),
    CONSTRAINT repositories_names_ck
        CHECK (
            btrim(owner) <> '' AND btrim(name) <> ''
            AND full_name = owner || '/' || name
        )
);

CREATE UNIQUE INDEX repositories_provider_full_name_ci_idx
    ON review_agent.repositories (provider, lower(full_name));

CREATE TABLE review_agent.pull_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id BIGINT NOT NULL,
    number INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT pull_requests_repository_fk
        FOREIGN KEY (repository_id) REFERENCES review_agent.repositories(id),
    CONSTRAINT pull_requests_repository_number_uk
        UNIQUE (repository_id, number),
    CONSTRAINT pull_requests_repository_identity_uk
        UNIQUE (id, repository_id),
    CONSTRAINT pull_requests_number_ck CHECK (number > 0)
);

CREATE TABLE review_agent.review_subjects (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pull_request_id BIGINT NOT NULL,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    resolved_config_schema_version SMALLINT NOT NULL,
    resolved_config JSONB NOT NULL,
    resolved_config_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT review_subjects_pull_request_fk
        FOREIGN KEY (pull_request_id) REFERENCES review_agent.pull_requests(id),
    CONSTRAINT review_subjects_identity_uk
        UNIQUE (
            pull_request_id, base_sha, head_sha, policy_revision,
            resolved_config_schema_version, resolved_config_hash
        ),
    CONSTRAINT review_subjects_pull_request_identity_uk
        UNIQUE (id, pull_request_id),
    CONSTRAINT review_subjects_sha_ck
        CHECK (
            base_sha ~ '^[0-9a-f]{40,64}$'
            AND head_sha ~ '^[0-9a-f]{40,64}$'
            AND resolved_config_hash ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT review_subjects_policy_revision_ck
        CHECK (policy_revision <> ''),
    CONSTRAINT review_subjects_resolved_config_ck
        CHECK (
            resolved_config_schema_version > 0
            AND jsonb_typeof(resolved_config) = 'object'
        )
);

CREATE TABLE review_agent.review_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pull_request_id BIGINT NOT NULL,
    review_subject_id BIGINT NOT NULL,
    request_key TEXT NOT NULL,
    trigger_comment_id BIGINT,
    trigger_user TEXT,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    findings_count INTEGER,
    changed_files_reported INTEGER,
    changed_file_registration_complete BOOLEAN NOT NULL DEFAULT false,
    failure_code TEXT,
    failure_status_comment_id BIGINT,
    failure_status_posted_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    CONSTRAINT review_runs_pull_request_fk
        FOREIGN KEY (pull_request_id) REFERENCES review_agent.pull_requests(id),
    CONSTRAINT review_runs_subject_pull_request_fk
        FOREIGN KEY (review_subject_id, pull_request_id)
        REFERENCES review_agent.review_subjects(id, pull_request_id),
    CONSTRAINT review_runs_pull_request_identity_uk
        UNIQUE (id, pull_request_id),
    CONSTRAINT review_runs_request_key_uk UNIQUE (request_key),
    CONSTRAINT review_runs_request_key_ck
        CHECK (btrim(request_key) <> '' AND char_length(request_key) <= 500),
    CONSTRAINT review_runs_trigger_comment_id_ck
        CHECK (trigger_comment_id IS NULL OR trigger_comment_id > 0),
    CONSTRAINT review_runs_findings_count_ck
        CHECK (findings_count IS NULL OR findings_count >= 0),
    CONSTRAINT review_runs_changed_files_ck
        CHECK (changed_files_reported IS NULL OR changed_files_reported >= 0),
    CONSTRAINT review_runs_failure_status_comment_id_ck
        CHECK (failure_status_comment_id IS NULL OR failure_status_comment_id > 0),
    CONSTRAINT review_runs_failure_status_ck
        CHECK (
            (failure_status_comment_id IS NULL AND failure_status_posted_at IS NULL)
            OR (
                failure_status_comment_id IS NOT NULL
                AND failure_status_posted_at IS NOT NULL
                AND status IN ('failed', 'superseded')
                AND failure_status_posted_at >= completed_at
            )
        ),
    CONSTRAINT review_runs_lifecycle_ck
        CHECK (
            (
                status = 'running'
                AND phase IN (
                    'accepted', 'fetching_pr', 'collecting_diff', 'reviewing',
                    'rendering', 'publishing'
                )
                AND completed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'completed'
                AND phase = 'posted'
                AND completed_at IS NOT NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'failed'
                AND phase = 'failed'
                AND completed_at IS NOT NULL
                AND failure_code IS NOT NULL
                AND btrim(failure_code) <> ''
            )
            OR (
                status = 'superseded'
                AND phase = 'superseded'
                AND completed_at IS NOT NULL
                AND failure_code = 'snapshot_superseded'
            )
        ),
    CONSTRAINT review_runs_timestamps_ck
        CHECK (
            last_heartbeat_at >= started_at
            AND (
                completed_at IS NULL
                OR (
                    completed_at >= started_at
                    AND last_heartbeat_at <= completed_at
                )
            )
        )
);

CREATE UNIQUE INDEX review_runs_active_pull_request_idx
    ON review_agent.review_runs (pull_request_id)
    WHERE status = 'running';
CREATE INDEX review_runs_pull_request_started_idx
    ON review_agent.review_runs (pull_request_id, started_at DESC);
CREATE INDEX review_runs_started_idx
    ON review_agent.review_runs (started_at DESC, id DESC);
CREATE INDEX review_runs_active_heartbeat_idx
    ON review_agent.review_runs (last_heartbeat_at, id)
    WHERE status = 'running';

CREATE TABLE review_agent.review_run_files (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    path TEXT NOT NULL,
    change_status TEXT,
    previous_path TEXT,
    is_changed_path BOOLEAN NOT NULL DEFAULT false,
    domain TEXT,
    review_mode TEXT NOT NULL DEFAULT 'normal',
    diff_state TEXT NOT NULL DEFAULT 'unseen',
    unavailable_reason TEXT,
    registered_at TIMESTAMPTZ NOT NULL,
    diff_observed_at TIMESTAMPTZ,
    CONSTRAINT review_run_files_run_fk
        FOREIGN KEY (review_run_id) REFERENCES review_agent.review_runs(id),
    CONSTRAINT review_run_files_run_path_uk UNIQUE (review_run_id, path),
    CONSTRAINT review_run_files_path_ck
        CHECK (
            path <> '' AND char_length(path) <= 500
            AND path !~ '^/' AND path !~ '/$' AND path !~ '//'
            AND position(chr(92) in path) = 0
            AND path !~ '(^|/)\.\.?(/|$)'
            AND (
                previous_path IS NULL
                OR (
                    previous_path <> '' AND char_length(previous_path) <= 500
                    AND previous_path !~ '^/' AND previous_path !~ '/$'
                    AND previous_path !~ '//'
                    AND position(chr(92) in previous_path) = 0
                    AND previous_path !~ '(^|/)\.\.?(/|$)'
                )
            )
        ),
    CONSTRAINT review_run_files_diff_state_ck
        CHECK (
            diff_state IN ('unseen', 'complete', 'truncated', 'unavailable')
            AND (
                (diff_state = 'unseen' AND diff_observed_at IS NULL)
                OR (
                    diff_state <> 'unseen'
                    AND diff_observed_at IS NOT NULL
                    AND diff_observed_at >= registered_at
                )
            )
        ),
    CONSTRAINT review_run_files_unavailable_reason_ck
        CHECK (
            (
                diff_state = 'unavailable'
                AND unavailable_reason IS NOT NULL
                AND btrim(unavailable_reason) <> ''
            )
            OR (
                diff_state <> 'unavailable'
                AND unavailable_reason IS NULL
            )
        )
);

CREATE TABLE review_agent.review_file_reads (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_file_id BIGINT NOT NULL,
    side TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT review_file_reads_file_fk
        FOREIGN KEY (review_run_file_id)
        REFERENCES review_agent.review_run_files(id),
    CONSTRAINT review_file_reads_range_uk
        UNIQUE (review_run_file_id, side, start_line, end_line),
    CONSTRAINT review_file_reads_side_ck CHECK (side IN ('base', 'head')),
    CONSTRAINT review_file_reads_line_ck
        CHECK (start_line > 0 AND end_line >= start_line)
);

CREATE TABLE review_agent.coach_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id BIGINT,
    source_event_set_id TEXT NOT NULL,
    source_snapshot_id TEXT,
    proposal_set_id TEXT NOT NULL,
    events_considered INTEGER NOT NULL,
    artifact_dir TEXT,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT coach_runs_repository_fk
        FOREIGN KEY (repository_id) REFERENCES review_agent.repositories(id),
    CONSTRAINT coach_runs_identifiers_ck
        CHECK (
            source_event_set_id ~ '^sha256:[0-9a-f]{64}$'
            AND (
                source_snapshot_id IS NULL
                OR source_snapshot_id ~ '^sha256:[0-9a-f]{64}$'
            )
            AND proposal_set_id ~ '^sha256:[0-9a-f]{64}$'
        ),
    CONSTRAINT coach_runs_counts_ck
        CHECK (events_considered >= 0)
);

CREATE INDEX coach_runs_repository_recorded_idx
    ON review_agent.coach_runs (repository_id, recorded_at DESC);

CREATE TABLE review_agent.coach_candidates (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    coach_run_id BIGINT NOT NULL,
    candidate_key TEXT NOT NULL,
    target_owner TEXT NOT NULL,
    suggested_route TEXT NOT NULL,
    event_type TEXT NOT NULL,
    independent_episode_count INTEGER NOT NULL,
    evidence_event_ids TEXT[] NOT NULL,
    evidence_events_total INTEGER NOT NULL,
    CONSTRAINT coach_candidates_run_fk
        FOREIGN KEY (coach_run_id) REFERENCES review_agent.coach_runs(id),
    CONSTRAINT coach_candidates_run_key_uk
        UNIQUE (coach_run_id, candidate_key),
    CONSTRAINT coach_candidates_counts_ck
        CHECK (
            independent_episode_count >= 1
            AND evidence_events_total >= cardinality(evidence_event_ids)
        ),
    CONSTRAINT coach_candidates_evidence_ck
        CHECK (
            cardinality(evidence_event_ids) > 0
            AND array_position(evidence_event_ids, NULL) IS NULL
        )
);

CREATE TABLE review_agent.finding_identities (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id BIGINT NOT NULL,
    fingerprint TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    path TEXT NOT NULL,
    symbol TEXT,
    anchor TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT finding_identities_repository_fk
        FOREIGN KEY (repository_id) REFERENCES review_agent.repositories(id),
    CONSTRAINT finding_identities_repository_fingerprint_uk
        UNIQUE (repository_id, fingerprint),
    CONSTRAINT finding_identities_repository_identity_uk
        UNIQUE (id, repository_id),
    CONSTRAINT finding_identities_fingerprint_ck
        CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT finding_identities_rule_id_ck
        CHECK (rule_id ~ '^[a-z0-9][a-z0-9._-]{2,80}$'),
    CONSTRAINT finding_identities_path_ck
        CHECK (
            path <> '' AND char_length(path) <= 500
            AND path !~ '^/' AND path !~ '/$' AND path !~ '//'
            AND position(chr(92) in path) = 0
            AND path !~ '(^|/)\.\.?(/|$)'
        ),
    CONSTRAINT finding_identities_anchor_ck CHECK (anchor <> '')
);

CREATE INDEX finding_identities_repository_path_idx
    ON review_agent.finding_identities (repository_id, path, last_seen_at DESC);
CREATE INDEX finding_identities_repository_seen_idx
    ON review_agent.finding_identities (repository_id, last_seen_at DESC);

CREATE TABLE review_agent.finding_occurrences (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    pull_request_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    finding_id BIGINT NOT NULL,
    line INTEGER NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    publication_score INTEGER NOT NULL,
    confidence NUMERIC(5,4) NOT NULL,
    context_hash TEXT NOT NULL,
    evidence TEXT NOT NULL,
    disproof_checks TEXT NOT NULL,
    impact TEXT NOT NULL,
    smallest_fix TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT finding_occurrences_run_pull_request_fk
        FOREIGN KEY (review_run_id, pull_request_id)
        REFERENCES review_agent.review_runs(id, pull_request_id),
    CONSTRAINT finding_occurrences_pull_request_repository_fk
        FOREIGN KEY (pull_request_id, repository_id)
        REFERENCES review_agent.pull_requests(id, repository_id),
    CONSTRAINT finding_occurrences_finding_repository_fk
        FOREIGN KEY (finding_id, repository_id)
        REFERENCES review_agent.finding_identities(id, repository_id),
    CONSTRAINT finding_occurrences_run_finding_uk
        UNIQUE (review_run_id, finding_id),
    CONSTRAINT finding_occurrences_identity_uk UNIQUE (id, finding_id),
    CONSTRAINT finding_occurrences_review_run_identity_uk
        UNIQUE (id, review_run_id),
    CONSTRAINT finding_occurrences_provenance_uk
        UNIQUE (id, review_run_id, finding_id, pull_request_id),
    CONSTRAINT finding_occurrences_line_ck CHECK (line > 0),
    CONSTRAINT finding_occurrences_title_ck CHECK (title <> ''),
    CONSTRAINT finding_occurrences_severity_ck
        CHECK (severity IN ('Critical', 'High', 'Medium', 'Low')),
    CONSTRAINT finding_occurrences_category_ck
        CHECK (
            category IN (
                'security', 'correctness', 'reliability', 'contracts', 'tests',
                'maintainability', 'performance', 'migration'
            )
        ),
    CONSTRAINT finding_occurrences_score_ck
        CHECK (publication_score >= 0 AND publication_score <= 10),
    CONSTRAINT finding_occurrences_confidence_ck
        CHECK (confidence >= 0.0000 AND confidence <= 1.0000),
    CONSTRAINT finding_occurrences_context_hash_ck
        CHECK (context_hash ~ '^[0-9a-f]{40,64}$'),
    CONSTRAINT finding_occurrences_evidence_ck
        CHECK (
            evidence <> '' AND disproof_checks <> '' AND impact <> ''
            AND smallest_fix <> ''
        )
);

CREATE INDEX finding_occurrences_finding_seen_idx
    ON review_agent.finding_occurrences (finding_id, observed_at DESC);

CREATE TABLE review_agent.finding_suggestions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_occurrence_id BIGINT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    expected_hash TEXT NOT NULL,
    replacement_text TEXT NOT NULL,
    suggestion_key TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT finding_suggestions_occurrence_fk
        FOREIGN KEY (finding_occurrence_id)
        REFERENCES review_agent.finding_occurrences(id),
    CONSTRAINT finding_suggestions_occurrence_uk UNIQUE (finding_occurrence_id),
    CONSTRAINT finding_suggestions_lines_ck
        CHECK (start_line > 0 AND end_line >= start_line),
    CONSTRAINT finding_suggestions_expected_hash_ck
        CHECK (expected_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT finding_suggestions_key_ck
        CHECK (suggestion_key ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX finding_suggestions_key_idx
    ON review_agent.finding_suggestions (suggestion_key);

CREATE TABLE review_agent.finding_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_id BIGINT NOT NULL,
    finding_occurrence_id BIGINT,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    context_hash TEXT,
    adr_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    CONSTRAINT finding_decisions_finding_fk
        FOREIGN KEY (finding_id) REFERENCES review_agent.finding_identities(id),
    CONSTRAINT finding_decisions_occurrence_finding_fk
        FOREIGN KEY (finding_occurrence_id, finding_id)
        REFERENCES review_agent.finding_occurrences(id, finding_id),
    CONSTRAINT finding_decisions_decision_ck
        CHECK (
            decision IN (
                'false_positive', 'intentional_by_design', 'accepted_risk',
                'duplicate', 'resolved', 'reopen'
            )
        ),
    CONSTRAINT finding_decisions_text_ck
        CHECK (btrim(reason) <> '' AND btrim(actor) <> ''),
    CONSTRAINT finding_decisions_context_hash_ck
        CHECK (context_hash IS NULL OR context_hash ~ '^[0-9a-f]{40,64}$'),
    CONSTRAINT finding_decisions_governance_ck
        CHECK (
            (
                decision IN (
                    'false_positive', 'intentional_by_design',
                    'accepted_risk', 'duplicate'
                )
                AND context_hash IS NOT NULL
                AND expires_at IS NOT NULL
                AND expires_at >= created_at + INTERVAL '1 day'
                AND expires_at <= created_at + INTERVAL '3650 days'
            )
            OR (
                decision IN ('resolved', 'reopen')
                AND expires_at IS NULL
            )
        ),
    CONSTRAINT finding_decisions_adr_ck
        CHECK (
            (adr_id IS NULL OR btrim(adr_id) <> '')
            AND (
                decision <> 'intentional_by_design'
                OR (adr_id IS NOT NULL AND btrim(adr_id) <> '')
            )
        )
);

CREATE INDEX finding_decisions_finding_created_idx
    ON review_agent.finding_decisions (finding_id, created_at DESC);

CREATE TABLE review_agent.verification_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    provider TEXT,
    model TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    bundle_hash TEXT,
    failure_code TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    CONSTRAINT verification_runs_review_run_fk
        FOREIGN KEY (review_run_id) REFERENCES review_agent.review_runs(id),
    CONSTRAINT verification_runs_review_run_identity_uk
        UNIQUE (id, review_run_id),
    CONSTRAINT verification_runs_mode_ck
        CHECK (mode IN ('shadow', 'advise', 'gate')),
    CONSTRAINT verification_runs_status_ck
        CHECK (status IN ('skipped', 'unavailable', 'running', 'completed', 'failed')),
    CONSTRAINT verification_runs_bundle_hash_ck
        CHECK (bundle_hash IS NULL OR bundle_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT verification_runs_lifecycle_ck
        CHECK (
            (
                status = 'running' AND completed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status IN ('skipped', 'completed') AND completed_at IS NOT NULL
                AND failure_code IS NULL
            )
            OR (
                status IN ('unavailable', 'failed') AND completed_at IS NOT NULL
                AND failure_code IS NOT NULL AND btrim(failure_code) <> ''
            )
        ),
    CONSTRAINT verification_runs_timestamps_ck
        CHECK (completed_at IS NULL OR completed_at >= started_at)
);

CREATE INDEX verification_runs_review_run_idx
    ON review_agent.verification_runs (review_run_id, id DESC);

CREATE TABLE review_agent.candidate_verifications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    verification_run_id BIGINT NOT NULL,
    review_run_id BIGINT NOT NULL,
    finding_occurrence_id BIGINT NOT NULL,
    verdict TEXT NOT NULL,
    confidence NUMERIC(5,4) NOT NULL,
    counter_evidence TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT candidate_verifications_verification_review_run_fk
        FOREIGN KEY (verification_run_id, review_run_id)
        REFERENCES review_agent.verification_runs(id, review_run_id),
    CONSTRAINT candidate_verifications_occurrence_review_run_fk
        FOREIGN KEY (finding_occurrence_id, review_run_id)
        REFERENCES review_agent.finding_occurrences(id, review_run_id),
    CONSTRAINT candidate_verifications_attempt_occurrence_uk
        UNIQUE (verification_run_id, finding_occurrence_id),
    CONSTRAINT candidate_verifications_verdict_ck
        CHECK (verdict IN ('confirmed', 'refuted', 'needs_more_evidence')),
    CONSTRAINT candidate_verifications_confidence_ck
        CHECK (confidence >= 0.0000 AND confidence <= 1.0000),
    CONSTRAINT candidate_verifications_refutation_ck
        CHECK (
            (counter_evidence IS NULL OR btrim(counter_evidence) <> '')
            AND (
                verdict <> 'refuted'
                OR (
                    counter_evidence IS NOT NULL
                    AND btrim(counter_evidence) <> ''
                )
            )
        )
);

CREATE TABLE review_agent.candidate_reconciliations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    finding_occurrence_id BIGINT NOT NULL,
    verification_run_id BIGINT,
    final_decision TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT candidate_reconciliations_occurrence_review_run_fk
        FOREIGN KEY (finding_occurrence_id, review_run_id)
        REFERENCES review_agent.finding_occurrences(id, review_run_id),
    CONSTRAINT candidate_reconciliations_verification_review_run_fk
        FOREIGN KEY (verification_run_id, review_run_id)
        REFERENCES review_agent.verification_runs(id, review_run_id),
    CONSTRAINT candidate_reconciliations_run_occurrence_uk
        UNIQUE (review_run_id, finding_occurrence_id),
    CONSTRAINT candidate_reconciliations_decision_ck
        CHECK (final_decision IN ('publish', 'drop')),
    CONSTRAINT candidate_reconciliations_reason_ck
        CHECK (
            (reason IS NULL OR btrim(reason) <> '')
            AND (
                final_decision <> 'drop'
                OR (reason IS NOT NULL AND btrim(reason) <> '')
            )
        )
);

CREATE TABLE review_agent.pull_request_finding_references (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pull_request_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    finding_id BIGINT NOT NULL,
    local_reference TEXT NOT NULL,
    first_assigned_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT pull_request_finding_references_pull_request_repository_fk
        FOREIGN KEY (pull_request_id, repository_id)
        REFERENCES review_agent.pull_requests(id, repository_id),
    CONSTRAINT pull_request_finding_references_finding_repository_fk
        FOREIGN KEY (finding_id, repository_id)
        REFERENCES review_agent.finding_identities(id, repository_id),
    CONSTRAINT pull_request_finding_references_finding_uk
        UNIQUE (pull_request_id, finding_id),
    CONSTRAINT pull_request_finding_references_local_reference_uk
        UNIQUE (pull_request_id, local_reference),
    CONSTRAINT pull_request_finding_references_mapping_uk
        UNIQUE (pull_request_id, finding_id, local_reference),
    CONSTRAINT pull_request_finding_references_local_reference_ck
        CHECK (local_reference ~ '^F[1-9][0-9]*$')
);

CREATE TABLE review_agent.publications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pull_request_id BIGINT NOT NULL,
    review_run_id BIGINT NOT NULL,
    review_number INTEGER NOT NULL,
    publication_key TEXT NOT NULL,
    rendered_markdown TEXT NOT NULL,
    rendered_blocks_schema_version SMALLINT NOT NULL,
    rendered_blocks JSONB NOT NULL,
    rendered_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'generated',
    generated_at TIMESTAMPTZ NOT NULL,
    posting_started_at TIMESTAMPTZ,
    posted_at TIMESTAMPTZ,
    publish_failed_at TIMESTAMPTZ,
    failure_code TEXT,
    superseded_at TIMESTAMPTZ,
    superseded_by_publication_id BIGINT,
    supersession_rendered_at TIMESTAMPTZ,
    supersession_failure_code TEXT,
    CONSTRAINT publications_pull_request_fk
        FOREIGN KEY (pull_request_id) REFERENCES review_agent.pull_requests(id),
    CONSTRAINT publications_review_run_pull_request_fk
        FOREIGN KEY (review_run_id, pull_request_id)
        REFERENCES review_agent.review_runs(id, pull_request_id),
    CONSTRAINT publications_review_run_uk UNIQUE (review_run_id),
    CONSTRAINT publications_review_number_uk
        UNIQUE (pull_request_id, review_number),
    CONSTRAINT publications_pull_request_identity_uk
        UNIQUE (id, pull_request_id),
    CONSTRAINT publications_run_identity_uk
        UNIQUE (id, review_run_id, pull_request_id),
    CONSTRAINT publications_key_uk UNIQUE (publication_key),
    CONSTRAINT publications_superseded_by_pull_request_fk
        FOREIGN KEY (superseded_by_publication_id, pull_request_id)
        REFERENCES review_agent.publications(id, pull_request_id),
    CONSTRAINT publications_review_number_ck CHECK (review_number > 0),
    CONSTRAINT publications_key_ck
        CHECK (publication_key ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT publications_blocks_ck
        CHECK (
            rendered_blocks_schema_version > 0
            AND jsonb_typeof(rendered_blocks) = 'array'
        ),
    CONSTRAINT publications_rendered_hash_ck
        CHECK (rendered_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT publications_status_ck
        CHECK (status IN ('generated', 'posting', 'posted', 'publish_failed', 'stale')),
    CONSTRAINT publications_state_timestamps_ck
        CHECK (
            (
                status = 'generated' AND posting_started_at IS NULL
                AND posted_at IS NULL AND publish_failed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'posting' AND posting_started_at IS NOT NULL
                AND posted_at IS NULL AND publish_failed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'posted' AND posting_started_at IS NOT NULL
                AND posted_at IS NOT NULL
                AND publish_failed_at IS NULL AND failure_code IS NULL
            )
            OR (
                status = 'publish_failed' AND posting_started_at IS NOT NULL
                AND publish_failed_at IS NOT NULL
                AND posted_at IS NULL AND failure_code IS NOT NULL
                AND btrim(failure_code) <> ''
            )
            OR (
                status = 'stale' AND posting_started_at IS NOT NULL
                AND posted_at IS NULL AND publish_failed_at IS NOT NULL
                AND failure_code IS NOT NULL AND btrim(failure_code) <> ''
            )
        ),
    CONSTRAINT publications_timestamps_ck
        CHECK (
            (posting_started_at IS NULL OR posting_started_at >= generated_at)
            AND (posted_at IS NULL OR posted_at >= posting_started_at)
            AND (
                publish_failed_at IS NULL
                OR publish_failed_at >= posting_started_at
            )
            AND (superseded_at IS NULL OR superseded_at >= posted_at)
            AND (
                supersession_rendered_at IS NULL
                OR supersession_rendered_at >= superseded_at
            )
        ),
    CONSTRAINT publications_supersession_ck
        CHECK (
            (
                superseded_at IS NULL
                AND superseded_by_publication_id IS NULL
                AND supersession_rendered_at IS NULL
                AND supersession_failure_code IS NULL
            )
            OR (
                superseded_at IS NOT NULL
                AND superseded_by_publication_id IS NOT NULL
                AND status = 'posted'
                AND superseded_by_publication_id <> id
                AND (
                    supersession_failure_code IS NULL
                    OR btrim(supersession_failure_code) <> ''
                )
            )
        )
);

CREATE UNIQUE INDEX publications_current_posted_idx
    ON review_agent.publications (pull_request_id)
    WHERE status = 'posted' AND superseded_by_publication_id IS NULL;

CREATE TABLE review_agent.publication_parts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_id BIGINT NOT NULL,
    part_type TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    external_id BIGINT,
    payload_schema_version SMALLINT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    posting_started_at TIMESTAMPTZ,
    posted_at TIMESTAMPTZ,
    failure_at TIMESTAMPTZ,
    failure_code TEXT,
    CONSTRAINT publication_parts_publication_fk
        FOREIGN KEY (publication_id) REFERENCES review_agent.publications(id),
    CONSTRAINT publication_parts_identity_uk
        UNIQUE (publication_id, part_type, part_number),
    CONSTRAINT publication_parts_type_ck
        CHECK (part_type IN ('summary', 'continuation', 'suggestion_review')),
    CONSTRAINT publication_parts_number_ck CHECK (part_number > 0),
    CONSTRAINT publication_parts_external_id_ck
        CHECK (external_id IS NULL OR external_id > 0),
    CONSTRAINT publication_parts_payload_ck
        CHECK (
            payload_schema_version > 0
            AND jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 131072
            AND payload_hash ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT publication_parts_status_ck
        CHECK (status IN ('pending', 'posting', 'posted', 'publish_failed', 'stale')),
    CONSTRAINT publication_parts_state_ck
        CHECK (
            (
                status = 'pending' AND external_id IS NULL
                AND posting_started_at IS NULL AND posted_at IS NULL
                AND failure_at IS NULL AND failure_code IS NULL
            )
            OR (
                status = 'posting' AND external_id IS NULL
                AND posting_started_at IS NOT NULL AND posted_at IS NULL
                AND failure_at IS NULL AND failure_code IS NULL
            )
            OR (
                status = 'posted' AND external_id IS NOT NULL
                AND posting_started_at IS NOT NULL AND posted_at IS NOT NULL
                AND failure_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'publish_failed' AND posting_started_at IS NOT NULL
                AND posted_at IS NULL
                AND failure_at IS NOT NULL AND failure_code IS NOT NULL
                AND btrim(failure_code) <> ''
            )
            OR (
                status = 'stale' AND posting_started_at IS NOT NULL
                AND posted_at IS NULL AND failure_at IS NOT NULL
                AND failure_code IS NOT NULL AND btrim(failure_code) <> ''
            )
        ),
    CONSTRAINT publication_parts_timestamps_ck
        CHECK (
            (posted_at IS NULL OR posted_at >= posting_started_at)
            AND (failure_at IS NULL OR failure_at >= posting_started_at)
        )
);

COMMENT ON COLUMN review_agent.publication_parts.payload IS
    'Structured delivery input; 128 KiB is a storage guard and the publication planner enforces provider limits';
COMMENT ON COLUMN review_agent.publication_parts.payload_hash IS
    'SHA-256 of the UTF-8 RFC 8785 canonical JSON representation of payload';

CREATE TABLE review_agent.publication_findings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_id BIGINT NOT NULL,
    publication_review_run_id BIGINT NOT NULL,
    pull_request_id BIGINT NOT NULL,
    finding_id BIGINT NOT NULL,
    source_finding_occurrence_id BIGINT NOT NULL,
    source_review_run_id BIGINT NOT NULL,
    local_reference TEXT NOT NULL,
    outcome TEXT NOT NULL,
    outcome_evidence TEXT,
    CONSTRAINT publication_findings_publication_run_fk
        FOREIGN KEY (
            publication_id, publication_review_run_id, pull_request_id
        )
        REFERENCES review_agent.publications(id, review_run_id, pull_request_id),
    CONSTRAINT publication_findings_pull_request_mapping_fk
        FOREIGN KEY (pull_request_id, finding_id, local_reference)
        REFERENCES review_agent.pull_request_finding_references(
            pull_request_id, finding_id, local_reference
        ),
    CONSTRAINT publication_findings_source_occurrence_fk
        FOREIGN KEY (
            source_finding_occurrence_id, source_review_run_id, finding_id,
            pull_request_id
        )
        REFERENCES review_agent.finding_occurrences(
            id, review_run_id, finding_id, pull_request_id
        ),
    CONSTRAINT publication_findings_reference_uk
        UNIQUE (publication_id, local_reference),
    CONSTRAINT publication_findings_finding_uk
        UNIQUE (publication_id, finding_id),
    CONSTRAINT publication_findings_local_reference_ck
        CHECK (local_reference ~ '^F[1-9][0-9]*$'),
    CONSTRAINT publication_findings_outcome_ck
        CHECK (
            outcome IN (
                'current', 'resolved', 'invalidated', 'suppressed', 'not_checked'
            )
            AND (
                (
                    outcome = 'current'
                    AND publication_review_run_id = source_review_run_id
                    AND outcome_evidence IS NULL
                )
                OR (
                    outcome <> 'current'
                    AND outcome_evidence IS NOT NULL
                    AND btrim(outcome_evidence) <> ''
                )
            )
        )
);

CREATE TABLE review_agent.review_quality_feedback (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pull_request_id BIGINT NOT NULL,
    publication_id BIGINT NOT NULL,
    local_reference TEXT,
    category TEXT NOT NULL,
    reason TEXT,
    actor_user_id TEXT NOT NULL,
    actor_login TEXT,
    author_association TEXT,
    source_comment_id BIGINT,
    source_comment_url TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT review_quality_feedback_pull_request_fk
        FOREIGN KEY (pull_request_id) REFERENCES review_agent.pull_requests(id),
    CONSTRAINT review_quality_feedback_publication_pull_request_fk
        FOREIGN KEY (publication_id, pull_request_id)
        REFERENCES review_agent.publications(id, pull_request_id),
    CONSTRAINT review_quality_feedback_publication_reference_fk
        FOREIGN KEY (publication_id, local_reference)
        REFERENCES review_agent.publication_findings(
            publication_id, local_reference
        ),
    CONSTRAINT review_quality_feedback_local_reference_ck
        CHECK (local_reference IS NULL OR local_reference ~ '^F[1-9][0-9]*$'),
    CONSTRAINT review_quality_feedback_category_ck
        CHECK (
            category IN (
                'useful', 'too_verbose', 'unclear', 'too_speculative',
                'severity_too_high', 'severity_too_low',
                'remediation_impractical', 'missed_issue', 'scope_confusion'
            )
        ),
    CONSTRAINT review_quality_feedback_actor_ck CHECK (actor_user_id <> ''),
    CONSTRAINT review_quality_feedback_source_comment_id_ck
        CHECK (source_comment_id IS NULL OR source_comment_id > 0)
);

CREATE INDEX review_quality_feedback_pull_request_created_idx
    ON review_agent.review_quality_feedback (pull_request_id, created_at DESC);

CREATE TABLE review_agent.processed_feedback_events (
    event_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL DEFAULT 'pending',
    processed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT processed_feedback_events_outcome_ck
        CHECK (
            outcome IN ('pending', 'recorded', 'no_mapping', 'not_current', 'stale')
        )
);

CREATE TABLE review_agent.decision_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_decision_id BIGINT NOT NULL,
    actor_user_id TEXT NOT NULL,
    actor_login TEXT,
    author_association TEXT,
    allowlist_version TEXT NOT NULL,
    source_comment_id BIGINT NOT NULL,
    source_comment_url TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT decision_audit_decision_fk
        FOREIGN KEY (finding_decision_id)
        REFERENCES review_agent.finding_decisions(id),
    CONSTRAINT decision_audit_decision_uk UNIQUE (finding_decision_id),
    CONSTRAINT decision_audit_source_comment_uk UNIQUE (source_comment_id),
    CONSTRAINT decision_audit_actor_ck CHECK (actor_user_id <> ''),
    CONSTRAINT decision_audit_allowlist_version_ck
        CHECK (allowlist_version ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT decision_audit_source_comment_id_ck
        CHECK (source_comment_id > 0)
);
