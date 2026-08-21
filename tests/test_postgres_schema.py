from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "bootstrap"
    / "plugins"
    / "review_agent_tools"
    / "postgres_migrations"
    / "001_initial.sql"
)
CONTAINER = os.environ.get("REVIEW_AGENT_POSTGRES_CONTAINER", "")


def psql(
    sql: str, *, check: bool = True, database: str = "review_agent_test"
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "--interactive",
            CONTAINER,
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--quiet",
            "--username=postgres",
            f"--dbname={database}",
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr.strip() or result.stdout.strip())
    return result


@unittest.skipUnless(CONTAINER, "run through scripts/check_postgres_schema.sh")
class PostgreSQLSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not MIGRATION.is_file():
            raise AssertionError("PostgreSQL migration 001 is missing")
        applied = psql(MIGRATION.read_text(encoding="utf-8"), check=False)
        if applied.returncode != 0:
            raise AssertionError(applied.stderr.strip())

    def setUp(self) -> None:
        tables = psql(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'review_agent'
              AND tablename <> 'schema_migrations'
            ORDER BY tablename;
            """
        ).stdout.splitlines()
        qualified = ", ".join(f"review_agent.{table}" for table in tables)
        if qualified:
            psql(f"TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE;")

    def assert_rejected(self, sql: str, constraint: str) -> None:
        result = psql(sql, check=False)
        self.assertNotEqual(result.returncode, 0, "invalid state was accepted")
        self.assertIn(constraint, result.stderr)

    def repository(self, provider_id: int, full_name: str) -> int:
        owner, name = full_name.split("/", maxsplit=1)
        result = psql(
            """
            INSERT INTO review_agent.repositories (
                provider, provider_repository_id, owner, name, full_name,
                created_at, updated_at
            ) VALUES (
                'github', %d, '%s', '%s', '%s', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            RETURNING id;
            """
            % (provider_id, owner, name, full_name)
        )
        return int(result.stdout.strip())

    def pull_request(self, repository_id: int, number: int) -> int:
        result = psql(
            """
            INSERT INTO review_agent.pull_requests (
                repository_id, number, created_at
            ) VALUES (%d, %d, CURRENT_TIMESTAMP)
            RETURNING id;
            """
            % (repository_id, number)
        )
        return int(result.stdout.strip())

    def subject(self, pull_request_id: int, digit: str = "1") -> int:
        result = psql(
            """
            INSERT INTO review_agent.review_subjects (
                pull_request_id, base_sha, head_sha, policy_revision,
                resolved_config, resolved_config_hash, created_at
            ) VALUES (
                %d, '%s', '%s', 'profile@1', '{}'::jsonb, '%s', CURRENT_TIMESTAMP
            )
            RETURNING id;
            """
            % (pull_request_id, digit * 40, str(int(digit) + 1) * 40, "a" * 64)
        )
        return int(result.stdout.strip())

    def create_run(
        self, pull_request_id: int, subject_id: int, status: str = "running"
    ) -> int:
        phase = "accepted" if status == "running" else "posted"
        completed = "NULL" if status == "running" else "CURRENT_TIMESTAMP"
        result = psql(
            """
            INSERT INTO review_agent.review_runs (
                pull_request_id, review_subject_id, trigger_user, status, phase,
                started_at, last_heartbeat_at, completed_at
            ) VALUES (
                %d, %d, 'reviewer', '%s', '%s', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, %s
            )
            RETURNING id;
            """
            % (pull_request_id, subject_id, status, phase, completed)
        )
        return int(result.stdout.strip())

    def finding(self, repository_id: int, fingerprint: str = "f" * 64) -> int:
        result = psql(
            """
            INSERT INTO review_agent.finding_identities (
                repository_id, fingerprint, rule_id, path, anchor,
                first_seen_at, last_seen_at, occurrences
            ) VALUES (
                %d, '%s', 'correctness.rule', 'src/app.py', 'stable anchor',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1
            )
            RETURNING id;
            """
            % (repository_id, fingerprint)
        )
        return int(result.stdout.strip())

    def occurrence(self, run_id: int, finding_id: int) -> int:
        result = psql(
            """
            INSERT INTO review_agent.finding_occurrences (
                review_run_id, pull_request_id, repository_id, finding_id,
                line, title, severity, category,
                publication_score, confidence, context_hash, evidence,
                disproof_checks, impact, smallest_fix, introduced_by_diff,
                observed_at
            )
            SELECT
                rr.id, rr.pull_request_id, pr.repository_id, %d,
                12, 'State can be lost', 'High', 'correctness', 8,
                0.9500, '%s', 'Observed lost update', 'Checked all callers',
                'Review state is incomplete', 'Persist atomically', true,
                CURRENT_TIMESTAMP
            FROM review_agent.review_runs AS rr
            JOIN review_agent.pull_requests AS pr ON pr.id = rr.pull_request_id
            WHERE rr.id = %d
            RETURNING id;
            """
            % (finding_id, "b" * 64, run_id)
        )
        return int(result.stdout.strip())

    def test_repository_rename_preserves_pull_request_and_review_history(self) -> None:
        repository_id = self.repository(101, "team/old-name")
        pull_request_id = self.pull_request(repository_id, 123)
        subject_id = self.subject(pull_request_id)
        run_id = self.create_run(pull_request_id, subject_id, status="generated")

        psql(
            """
            UPDATE review_agent.repositories
            SET owner = 'platform', name = 'new-name',
                full_name = 'platform/new-name', updated_at = CURRENT_TIMESTAMP
            WHERE id = %d;
            """
            % repository_id
        )

        history = psql(
            """
            SELECT r.provider_repository_id, r.full_name, pr.number, rr.id
            FROM review_agent.review_runs AS rr
            JOIN review_agent.pull_requests AS pr ON pr.id = rr.pull_request_id
            JOIN review_agent.repositories AS r ON r.id = pr.repository_id
            WHERE rr.id = %d;
            """
            % run_id
        ).stdout.strip()
        self.assertEqual(history, f"101|platform/new-name|123|{run_id}")
        self.assert_rejected(
            """
            INSERT INTO review_agent.repositories (
                provider, provider_repository_id, owner, name, full_name,
                created_at, updated_at
            ) VALUES (
                'github', 101, 'other', 'copy', 'other/copy',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """,
            "repositories_provider_identity_uk",
        )

    def test_pull_request_numbers_and_active_runs_are_repository_scoped(self) -> None:
        first_repository = self.repository(201, "team/first")
        second_repository = self.repository(202, "team/second")
        first_pr = self.pull_request(first_repository, 7)
        second_pr = self.pull_request(second_repository, 7)
        first_run = self.create_run(first_pr, self.subject(first_pr, "1"))
        second_run = self.create_run(second_pr, self.subject(second_pr, "3"))

        self.assertNotEqual(first_pr, second_pr)
        self.assertNotEqual(first_run, second_run)
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_runs (
                pull_request_id, review_subject_id, trigger_user, status, phase,
                started_at, last_heartbeat_at
            ) VALUES (
                %d, %d, 'reviewer', 'running', 'accepted',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
            % (first_pr, self.subject(first_pr, "5")),
            "review_runs_active_pull_request_idx",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_runs (
                pull_request_id, review_subject_id, trigger_user, status, phase,
                started_at, last_heartbeat_at, completed_at
            ) VALUES (
                %d, %d, 'reviewer', 'generated', 'posted', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
            % (first_pr, self.subject(second_pr, "6")),
            "review_runs_subject_pull_request_fk",
        )

    def test_identical_fingerprints_are_independent_across_repositories(self) -> None:
        first_repository = self.repository(301, "team/first")
        second_repository = self.repository(302, "team/second")
        fingerprint = "c" * 64
        first_finding = self.finding(first_repository, fingerprint)
        second_finding = self.finding(second_repository, fingerprint)
        first_pr = self.pull_request(first_repository, 9)
        second_pr = self.pull_request(second_repository, 9)
        first_run = self.create_run(first_pr, self.subject(first_pr, "1"))
        second_run = self.create_run(second_pr, self.subject(second_pr, "3"))
        first_occurrence = self.occurrence(first_run, first_finding)
        second_occurrence = self.occurrence(second_run, second_finding)

        linked = psql(
            """
            SELECT fi.repository_id, fo.id
            FROM review_agent.finding_occurrences AS fo
            JOIN review_agent.finding_identities AS fi ON fi.id = fo.finding_id
            ORDER BY fi.repository_id;
            """
        ).stdout.splitlines()
        self.assertEqual(
            linked,
            [
                f"{first_repository}|{first_occurrence}",
                f"{second_repository}|{second_occurrence}",
            ],
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.pull_request_finding_references (
                pull_request_id, repository_id, finding_id, local_reference,
                first_assigned_at
            ) VALUES (%d, %d, %d, 'F1', CURRENT_TIMESTAMP);
            """
            % (first_pr, first_repository, second_finding),
            "pull_request_finding_references_finding_repository_fk",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.finding_occurrences (
                review_run_id, pull_request_id, repository_id, finding_id,
                line, title, severity, category, publication_score, confidence,
                context_hash, evidence, disproof_checks, impact, smallest_fix,
                introduced_by_diff, observed_at
            ) VALUES (
                %d, %d, %d, %d, 12, 'Wrong repository', 'High', 'correctness',
                8, 0.9500, '%s', 'evidence', 'checks', 'impact', 'fix', true,
                CURRENT_TIMESTAMP
            );
            """
            % (
                first_run,
                first_pr,
                first_repository,
                second_finding,
                "d" * 64,
            ),
            "finding_occurrences_finding_repository_fk",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.finding_suggestions (
                finding_occurrence_id, start_line, end_line, expected_hash,
                replacement_text, suggestion_key, recorded_at
            ) VALUES (
                999999, 12, 12, '%s', 'replacement', 'missing-parent',
                CURRENT_TIMESTAMP
            );
            """
            % ("d" * 64),
            "finding_suggestions_occurrence_fk",
        )

    def test_suggestions_preserve_empty_replacement_for_line_deletion(self) -> None:
        repository_id = self.repository(321, "team/deletion-suggestion")
        pull_request_id = self.pull_request(repository_id, 11)
        run_id = self.create_run(pull_request_id, self.subject(pull_request_id))
        finding_id = self.finding(repository_id)
        occurrence_id = self.occurrence(run_id, finding_id)

        stored_replacement = psql(
            """
            INSERT INTO review_agent.finding_suggestions (
                finding_occurrence_id, start_line, end_line, expected_hash,
                replacement_text, suggestion_key, recorded_at
            ) VALUES (
                %d, 12, 12, '%s', '', 'delete-obsolete-line',
                CURRENT_TIMESTAMP
            )
            RETURNING replacement_text;
            """
            % (occurrence_id, "d" * 64)
        ).stdout

        self.assertEqual(stored_replacement, "\n")

    def test_decisions_require_the_occurrence_to_match_the_finding(self) -> None:
        repository_id = self.repository(351, "team/decisions")
        pull_request_id = self.pull_request(repository_id, 10)
        run_id = self.create_run(pull_request_id, self.subject(pull_request_id))
        first_finding = self.finding(repository_id, "1" * 64)
        second_finding = self.finding(repository_id, "2" * 64)
        second_occurrence = self.occurrence(run_id, second_finding)

        self.assert_rejected(
            """
            INSERT INTO review_agent.finding_decisions (
                finding_id, finding_occurrence_id, decision, reason, actor,
                context_hash, created_at, expires_at
            ) VALUES (
                %d, %d, 'false_positive', 'Verified mismatch', 'reviewer',
                '%s', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '180 days'
            );
            """
            % (first_finding, second_occurrence, "3" * 64),
            "finding_decisions_occurrence_finding_fk",
        )

    def test_publication_membership_requires_matching_finding_and_occurrence(self) -> None:
        repository_id = self.repository(352, "team/membership")
        pull_request_id = self.pull_request(repository_id, 11)
        run_id = self.create_run(
            pull_request_id, self.subject(pull_request_id), status="generated"
        )
        first_finding = self.finding(repository_id, "3" * 64)
        second_finding = self.finding(repository_id, "4" * 64)
        second_occurrence = self.occurrence(run_id, second_finding)
        psql(
            """
            INSERT INTO review_agent.pull_request_finding_references (
                pull_request_id, repository_id, finding_id, local_reference,
                first_assigned_at
            ) VALUES (%d, %d, %d, 'F1', CURRENT_TIMESTAMP);
            """
            % (pull_request_id, repository_id, first_finding)
        )
        publication_id = int(
            psql(
                """
                INSERT INTO review_agent.publications (
                    pull_request_id, review_run_id, review_number,
                    publication_key, rendered_markdown, rendered_blocks,
                    rendered_hash, status, generated_at
                ) VALUES (
                    %d, %d, 1, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                    'generated', CURRENT_TIMESTAMP
                )
                RETURNING id;
                """
                % (pull_request_id, run_id, "4" * 64, "5" * 64)
            ).stdout.strip()
        )

        self.assert_rejected(
            """
            INSERT INTO review_agent.publication_findings (
                publication_id, pull_request_id, finding_id, finding_occurrence_id,
                local_reference, status
            ) VALUES (%d, %d, %d, %d, 'F1', 'current');
            """
            % (publication_id, pull_request_id, first_finding, second_occurrence),
            "publication_findings_occurrence_finding_fk",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publication_findings (
                publication_id, pull_request_id, finding_id, local_reference
            ) VALUES (%d, %d, %d, 'F9');
            """
            % (publication_id, pull_request_id, first_finding),
            "publication_findings_pull_request_mapping_fk",
        )
        psql(
            """
            INSERT INTO review_agent.publication_findings (
                publication_id, pull_request_id, finding_id, local_reference
            ) VALUES (%d, %d, %d, 'F1');
            """
            % (publication_id, pull_request_id, first_finding)
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_quality_feedback (
                pull_request_id, publication_id, head_sha, local_reference,
                category, actor_user_id, created_at
            ) VALUES (
                %d, %d, '%s', 'F9', 'useful', '42', CURRENT_TIMESTAMP
            );
            """
            % (pull_request_id, publication_id, "2" * 40),
            "review_quality_feedback_publication_reference_fk",
        )

    def test_verifier_and_reconciliation_parents_must_share_the_review_run(self) -> None:
        repository_id = self.repository(353, "team/verification")
        pull_request_id = self.pull_request(repository_id, 12)
        first_run = self.create_run(
            pull_request_id, self.subject(pull_request_id, "1"), status="generated"
        )
        second_run = self.create_run(
            pull_request_id, self.subject(pull_request_id, "3"), status="generated"
        )
        finding_id = self.finding(repository_id, "6" * 64)
        occurrence_id = self.occurrence(second_run, finding_id)
        verification_run_id = int(
            psql(
                """
                INSERT INTO review_agent.verification_runs (
                    review_run_id, provider, model, mode, status, started_at,
                    completed_at
                ) VALUES (
                    %d, 'claude', 'verifier', 'shadow', 'completed',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                RETURNING id;
                """
                % first_run
            ).stdout.strip()
        )

        self.assert_rejected(
            """
            INSERT INTO review_agent.candidate_verifications (
                verification_run_id, review_run_id, finding_occurrence_id, verdict,
                confidence, created_at
            ) VALUES (%d, %d, %d, 'confirmed', 0.9000, CURRENT_TIMESTAMP);
            """
            % (verification_run_id, second_run, occurrence_id),
            "candidate_verifications_verification_review_run_fk",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.candidate_reconciliations (
                review_run_id, finding_occurrence_id, verification_run_id,
                final_decision, created_at
            ) VALUES (%d, %d, %d, 'publish', CURRENT_TIMESTAMP);
            """
            % (second_run, occurrence_id, verification_run_id),
            "candidate_reconciliations_verification_review_run_fk",
        )

    def test_file_read_ranges_validate_deduplicate_and_order(self) -> None:
        repository_id = self.repository(401, "team/files")
        pull_request_id = self.pull_request(repository_id, 12)
        run_id = self.create_run(pull_request_id, self.subject(pull_request_id))
        run_file_id = int(
            psql(
                """
                INSERT INTO review_agent.review_run_files (
                    review_run_id, path, change_status, is_changed_path,
                    diff_state, first_accessed_at, last_accessed_at
                ) VALUES (
                    %d, 'src/app.py', 'modified', true, 'complete',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                RETURNING id;
                """
                % run_id
            ).stdout.strip()
        )
        psql(
            """
            INSERT INTO review_agent.review_file_reads (
                review_run_file_id, side, start_line, end_line, recorded_at
            ) VALUES
                (%d, 'head', 20, 25, CURRENT_TIMESTAMP),
                (%d, 'base', 4, 8, CURRENT_TIMESTAMP),
                (%d, 'head', 2, 3, CURRENT_TIMESTAMP);
            """
            % (run_file_id, run_file_id, run_file_id)
        )

        ordered = psql(
            """
            SELECT side || ':' || start_line || '-' || end_line
            FROM review_agent.review_file_reads
            WHERE review_run_file_id = %d
            ORDER BY side, start_line, end_line;
            """
            % run_file_id
        ).stdout.splitlines()
        self.assertEqual(ordered, ["base:4-8", "head:2-3", "head:20-25"])
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_file_reads (
                review_run_file_id, side, start_line, end_line, recorded_at
            ) VALUES (%d, 'head', 20, 25, CURRENT_TIMESTAMP);
            """
            % run_file_id,
            "review_file_reads_range_uk",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_file_reads (
                review_run_file_id, side, start_line, end_line, recorded_at
            ) VALUES (%d, 'head', 0, 2, CURRENT_TIMESTAMP);
            """
            % run_file_id,
            "review_file_reads_line_ck",
        )
        for invalid_path in ("src//bad.py", "src/bad.py/", "src\\bad.py"):
            self.assert_rejected(
                """
                INSERT INTO review_agent.review_run_files (
                    review_run_id, path, first_accessed_at, last_accessed_at
                ) VALUES (%d, '%s', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """
                % (run_id, invalid_path.replace("\\", "\\\\")),
                "review_run_files_path_ck",
            )
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_run_files (
                review_run_id, path, diff_state, first_accessed_at,
                last_accessed_at
            ) VALUES (
                %d, 'src/unavailable.py', 'unavailable', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            );
            """
            % run_id,
            "review_run_files_unavailable_reason_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_run_files (
                review_run_id, path, diff_state, unavailable_reason,
                first_accessed_at, last_accessed_at
            ) VALUES (
                %d, 'src/complete.py', 'complete', 'contradictory',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
            % run_id,
            "review_run_files_unavailable_reason_ck",
        )

    def test_publication_current_state_and_coach_scope_are_deduplicated(self) -> None:
        repository_id = self.repository(501, "team/publish")
        pull_request_id = self.pull_request(repository_id, 18)
        first_run = self.create_run(
            pull_request_id, self.subject(pull_request_id, "1"), status="generated"
        )
        second_run = self.create_run(
            pull_request_id, self.subject(pull_request_id, "3"), status="generated"
        )
        other_repository = self.repository(502, "team/other")
        other_pr = self.pull_request(other_repository, 18)
        self.assert_rejected(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at
            ) VALUES (
                %d, %d, 1, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'generated', CURRENT_TIMESTAMP
            );
            """
            % (other_pr, first_run, "a" * 64, "d" * 64),
            "publications_review_run_pull_request_fk",
        )
        publication_id = int(
            psql(
                """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at, posted_at
            ) VALUES (
                %d, %d, 1, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'posted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            ) RETURNING id;
            """
                % (pull_request_id, first_run, "b" * 64, "e" * 64)
            ).stdout.strip()
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publication_parts (
                publication_id, part_type, part_number, body_hash, status,
                posted_at
            ) VALUES (
                %d, 'summary', 1, '%s', 'posted', CURRENT_TIMESTAMP
            );
            """
            % (publication_id, "9" * 64),
            "publication_parts_state_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publication_parts (
                publication_id, part_type, part_number, body_hash, status,
                failure_at, failure_code
            ) VALUES (
                %d, 'summary', 1, '%s', 'publish_failed', CURRENT_TIMESTAMP, ''
            );
            """
            % (publication_id, "8" * 64),
            "publication_parts_state_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publication_parts (
                publication_id, part_type, part_number, body_hash, status
            ) VALUES (
                %d, 'continuation', 2, '%s', 'stale'
            );
            """
            % (publication_id, "7" * 64),
            "publication_parts_state_ck",
        )
        psql(
            """
            INSERT INTO review_agent.publication_parts (
                publication_id, part_type, part_number, body_hash, status,
                failure_at, failure_code
            ) VALUES (
                %d, 'continuation', 2, '%s', 'stale', CURRENT_TIMESTAMP,
                'stale_head'
            );
            """
            % (publication_id, "7" * 64)
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at, posted_at
            ) VALUES (
                %d, %d, 2, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'posted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
            % (pull_request_id, second_run, "c" * 64, "f" * 64),
            "publications_current_posted_idx",
        )

        other_run = self.create_run(
            other_pr, self.subject(other_pr, "5"), status="generated"
        )
        other_publication = int(
            psql(
                """
                INSERT INTO review_agent.publications (
                    pull_request_id, review_run_id, review_number,
                    publication_key, rendered_markdown, rendered_blocks,
                    rendered_hash, status, generated_at
                ) VALUES (
                    %d, %d, 1, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                    'generated', CURRENT_TIMESTAMP
                ) RETURNING id;
                """
                % (other_pr, other_run, "1" * 64, "2" * 64)
            ).stdout.strip()
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.review_quality_feedback (
                pull_request_id, publication_id, head_sha, category, actor_user_id,
                created_at
            ) VALUES (%d, %d, '%s', 'useful', '42', CURRENT_TIMESTAMP);
            """
            % (other_pr, publication_id, "6" * 40),
            "review_quality_feedback_publication_pull_request_fk",
        )
        self.assert_rejected(
            """
            UPDATE review_agent.publications
            SET superseded_at = CURRENT_TIMESTAMP,
                superseded_by_publication_id = %d
            WHERE id = %d;
            """
            % (other_publication, publication_id),
            "publications_superseded_by_pull_request_fk",
        )
        self.assert_rejected(
            """
            UPDATE review_agent.publications
            SET superseded_at = CURRENT_TIMESTAMP,
                superseded_by_publication_id = id
            WHERE id = %d;
            """
            % publication_id,
            "publications_supersession_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at
            ) VALUES (
                %d, %d, 2, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'generated', CURRENT_TIMESTAMP
            );
            """
            % (pull_request_id, second_run, "b" * 64, "3" * 64),
            "publications_key_uk",
        )

        candidate = """
            INSERT INTO review_agent.coach_candidates (
                repository_id, candidate_key, proposal_set_id,
                source_event_set_id, target_owner, suggested_route, event_type,
                independent_episode_count, evidence_event_ids,
                evidence_events_total, first_seen_at, last_seen_at, seen_count
            ) VALUES (
                %s, 'candidate', 'proposal', 'events', 'profile', 'route',
                'feedback', 1, '[]'::jsonb, 0, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, 1
            );
        """
        psql(candidate % "NULL")
        self.assert_rejected(
            candidate % "NULL", "coach_candidates_scope_key_uk"
        )
        psql(candidate % repository_id)
        self.assert_rejected(
            candidate % repository_id, "coach_candidates_scope_key_uk"
        )

    def test_governance_and_terminal_lifecycle_constraints_match_current_behavior(
        self,
    ) -> None:
        repository_id = self.repository(551, "team/lifecycle")
        pull_request_id = self.pull_request(repository_id, 19)
        run_id = self.create_run(
            pull_request_id, self.subject(pull_request_id), status="generated"
        )
        finding_id = self.finding(repository_id, "7" * 64)

        self.assert_rejected(
            """
            INSERT INTO review_agent.finding_decisions (
                finding_id, decision, reason, actor, context_hash, created_at,
                expires_at
            ) VALUES (
                %d, 'false_positive', 'not applicable', 'reviewer',
                '%s', CURRENT_TIMESTAMP, NULL
            );
            """
            % (finding_id, "6" * 64),
            "finding_decisions_governance_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.finding_decisions (
                finding_id, decision, reason, actor, context_hash, adr_id,
                created_at, expires_at
            ) VALUES (
                %d, 'intentional_by_design', 'documented design', 'reviewer',
                '%s', '', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP + INTERVAL '180 days'
            );
            """
            % (finding_id, "8" * 64),
            "finding_decisions_adr_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.verification_runs (
                review_run_id, mode, status, started_at
            ) VALUES (%d, 'shadow', 'completed', CURRENT_TIMESTAMP);
            """
            % run_id,
            "verification_runs_completed_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.finding_identities (
                repository_id, fingerprint, rule_id, path, anchor,
                first_seen_at, last_seen_at
            ) VALUES (
                %d, '%s', 'correctness.path', 'src//bad.py', 'anchor',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
            % (repository_id, "9" * 64),
            "finding_identities_path_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, generated_at
            ) VALUES (
                %d, %d, 1, 'not-a-recovery-key', 'review', '[]'::jsonb, '%s',
                CURRENT_TIMESTAMP
            );
            """
            % (pull_request_id, run_id, "a" * 64),
            "publications_key_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at, publish_failed_at, failure_code
            ) VALUES (
                %d, %d, 1, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'publish_failed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ''
            );
            """
            % (pull_request_id, run_id, "d" * 64, "e" * 64),
            "publications_state_timestamps_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at
            ) VALUES (
                %d, %d, 2, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'stale', CURRENT_TIMESTAMP
            );
            """
            % (pull_request_id, run_id, "f" * 64, "0" * 64),
            "publications_state_timestamps_ck",
        )
        psql(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks, rendered_hash, status,
                generated_at, publish_failed_at, failure_code
            ) VALUES (
                %d, %d, 2, 'sha256:%s', 'review', '[]'::jsonb, '%s',
                'stale', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'stale_head'
            );
            """
            % (pull_request_id, run_id, "f" * 64, "0" * 64)
        )
        occurrence_id = self.occurrence(run_id, finding_id)
        verification_run_id = int(
            psql(
                """
                INSERT INTO review_agent.verification_runs (
                    review_run_id, mode, status, started_at, completed_at
                ) VALUES (
                    %d, 'shadow', 'completed', CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                ) RETURNING id;
                """
                % run_id
            ).stdout.strip()
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.candidate_verifications (
                verification_run_id, review_run_id, finding_occurrence_id,
                verdict, confidence, counter_evidence, created_at
            ) VALUES (
                %d, %d, %d, 'refuted', 0.9000, '', CURRENT_TIMESTAMP
            );
            """
            % (verification_run_id, run_id, occurrence_id),
            "candidate_verifications_refutation_ck",
        )
        self.assert_rejected(
            """
            INSERT INTO review_agent.candidate_reconciliations (
                review_run_id, finding_occurrence_id, final_decision, reason,
                created_at
            ) VALUES (%d, %d, 'drop', '', CURRENT_TIMESTAMP);
            """
            % (run_id, occurrence_id),
            "candidate_reconciliations_reason_ck",
        )

    def test_feedback_outcomes_and_confidence_use_bounded_database_types(self) -> None:
        confidence = psql(
            """
            SELECT data_type || ':' || numeric_precision || ':' || numeric_scale
            FROM information_schema.columns
            WHERE table_schema = 'review_agent'
              AND table_name = 'finding_occurrences'
              AND column_name = 'confidence';
            """
        ).stdout.strip()
        self.assertEqual(confidence, "numeric:5:4")
        self.assert_rejected(
            """
            INSERT INTO review_agent.processed_feedback_events (
                event_id, outcome, processed_at
            ) VALUES ('event-1', 'anything-goes', CURRENT_TIMESTAMP);
            """,
            "processed_feedback_events_outcome_ck",
        )

    def test_migration_is_single_use_and_partial_failure_is_transactional(self) -> None:
        reapplied = psql(MIGRATION.read_text(encoding="utf-8"), check=False)
        self.assertNotEqual(reapplied.returncode, 0)
        self.assertIn('schema "review_agent" already exists', reapplied.stderr)
        self.assertEqual(
            psql("SELECT version FROM review_agent.schema_migrations;").stdout.strip(),
            "1",
        )

        database = "review_agent_partial"
        psql(f"DROP DATABASE IF EXISTS {database};", database="postgres")
        psql(f"CREATE DATABASE {database};", database="postgres")
        try:
            source = MIGRATION.read_text(encoding="utf-8")
            marker = "INSERT INTO review_agent.schema_migrations (version) VALUES (1);"
            self.assertEqual(source.count(marker), 1)
            late_failure = source.replace(
                marker,
                "SELECT 1 / 0;\n\n" + marker,
            )
            failed = psql(
                late_failure,
                check=False,
                database=database,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("division by zero", failed.stderr)
            self.assertEqual(
                psql(
                    """
                    SELECT coalesce(to_regnamespace('review_agent')::text, '');
                    """,
                    database=database,
                ).stdout.strip(),
                "",
            )
            self.assertEqual(
                psql(
                    "SELECT coalesce(to_regclass('review_agent.schema_migrations')::text, '');",
                    database=database,
                ).stdout.strip(),
                "",
            )
        finally:
            psql(
                f"DROP DATABASE IF EXISTS {database} WITH (FORCE);", database="postgres"
            )


if __name__ == "__main__":
    unittest.main()
