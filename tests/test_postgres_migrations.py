from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres import github_app  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
MIGRATIONS = (
    ROOT / "bootstrap" / "plugins" / "review_agent_tools" / "postgres_migrations"
)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLMigrationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")

    def test_applies_once_and_records_the_exact_source_checksum(self) -> None:
        with psycopg.connect(DSN) as connection:
            self.assertEqual(
                runner.apply_migrations(connection),
                (
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    8,
                    9,
                    10,
                    11,
                    12,
                    13,
                    14,
                    15,
                    16,
                    17,
                    18,
                    19,
                    20,
                    21,
                    22,
                    23,
                    24,
                    25,
                    26,
                    27,
                    28,
                    29,
                    30,
                    31,
                    32,
                    33,
                    34,
                    35,
                ),
            )
            self.assertEqual(runner.apply_migrations(connection), ())
            rows = connection.execute(
                """
                SELECT version, name, checksum, applied_at IS NOT NULL
                FROM review_agent.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            tables = connection.execute(
                "SELECT to_regclass('review_agent.repositories')::text, "
                "to_regclass('review_agent.review_jobs')::text, "
                "to_regclass('review_agent.github_app_installations')::text, "
                "to_regclass('review_agent.github_app_installation_events')::text, "
                "to_regclass('review_agent.github_app_repository_access')::text, "
                "to_regclass('review_agent.github_app_repository_access_events')::text, "
                "to_regclass('review_agent.github_webhook_deliveries')::text, "
                "to_regclass('review_agent.review_decision_snapshots')::text, "
                "to_regclass('review_agent.intentional_design_evidence')::text, "
                "to_regclass('review_agent.review_quality_feedback_triage')::text, "
                "to_regclass('review_agent.coach_intervention_outcomes')::text, "
                "to_regclass('review_agent.review_guidance_snapshots')::text"
            ).fetchone()

        self.assertEqual(
            rows,
            [
                (
                    version,
                    name,
                    hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest(),
                    True,
                )
                for version, name in (
                    (1, "001_initial.sql"),
                    (2, "002_review_jobs.sql"),
                    (3, "003_review_job_lifecycle.sql"),
                    (4, "004_publication_delivery_queue.sql"),
                    (5, "005_failure_status_delivery.sql"),
                    (6, "006_github_app_installations.sql"),
                    (7, "007_github_webhook_deliveries.sql"),
                    (8, "008_feedback_authorization_audit.sql"),
                    (9, "009_repository_decision_context.sql"),
                    (10, "010_intentional_design_evidence.sql"),
                    (11, "011_review_quality_feedback_triage.sql"),
                    (12, "012_coach_intervention_outcomes.sql"),
                    (13, "013_repository_guidance_context.sql"),
                    (14, "014_github_app_repository_activation.sql"),
                    (15, "015_diff_page_coverage.sql"),
                    (16, "016_finding_root_cause_groups.sql"),
                    (17, "017_admin_accounts.sql"),
                    (18, "018_admin_operations.sql"),
                    (19, "019_admin_run_actions.sql"),
                    (20, "020_deployment_settings.sql"),
                    (21, "021_settings_service_owners.sql"),
                    (22, "022_team_access.sql"),
                    (23, "023_audit_search.sql"),
                    (24, "024_model_connections.sql"),
                    (25, "025_model_capacity.sql"),
                    (26, "026_integrations.sql"),
                    (27, "027_console_identity.sql"),
                    (28, "028_console_registration.sql"),
                    (29, "029_review_purpose.sql"),
                    (30, "030_documentation_reviews.sql"),
                    (31, "031_documentation_check_publication.sql"),
                    (32, "032_documentation_operating_policy.sql"),
                    (33, "033_documentation_capability.sql"),
                    (34, "034_documentation_configuration.sql"),
                    (35, "035_documentation_admission.sql"),
                )
            ],
        )
        self.assertEqual(
            tables,
            (
                "review_agent.repositories",
                "review_agent.review_jobs",
                "review_agent.github_app_installations",
                "review_agent.github_app_installation_events",
                "review_agent.github_app_repository_access",
                "review_agent.github_app_repository_access_events",
                "review_agent.github_webhook_deliveries",
                "review_agent.review_decision_snapshots",
                "review_agent.intentional_design_evidence",
                "review_agent.review_quality_feedback_triage",
                "review_agent.coach_intervention_outcomes",
                "review_agent.review_guidance_snapshots",
            ),
        )

    def test_activation_migration_preserves_only_explicit_repository_denials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            previous = Path(temp)
            for source in sorted(MIGRATIONS.glob("*.sql")):
                if int(source.name[:3]) < 14:
                    shutil.copy2(source, previous / source.name)

            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection, directory=previous),
                    tuple(range(1, 14)),
                )
                installation_id = connection.execute(
                    """
                    INSERT INTO review_agent.github_app_installations (
                        provider_installation_id, account_id, account_login,
                        account_type, repository_selection, status,
                        contents_permission, issues_permission,
                        pull_requests_permission, created_at, updated_at
                    ) VALUES (
                        7001, 8001, 'Example-Org', 'organization', 'selected',
                        'active', 'read', 'write', 'write',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    RETURNING id
                    """
                ).fetchone()[0]
                repository_rows = connection.execute(
                    """
                    INSERT INTO review_agent.repositories (
                        provider, provider_repository_id, owner, name, full_name,
                        created_at, updated_at
                    ) VALUES
                        ('github', 9001, 'Example-Org', 'untouched',
                         'Example-Org/untouched', CURRENT_TIMESTAMP,
                         CURRENT_TIMESTAMP),
                        ('github', 9002, 'Example-Org', 'paused',
                         'Example-Org/paused', CURRENT_TIMESTAMP,
                         CURRENT_TIMESTAMP)
                    RETURNING id, provider_repository_id
                    """
                ).fetchall()
                repository_ids = {
                    provider_id: repository_id
                    for repository_id, provider_id in repository_rows
                }
                for provider_id, repository_id in repository_ids.items():
                    connection.execute(
                        """
                        INSERT INTO review_agent.github_app_repository_access (
                            repository_id, installation_id, access_state, enabled,
                            trigger_mode, profile_key, enabled_at, disabled_at,
                            updated_by, update_reason, updated_at
                        ) VALUES (
                            %s, %s, 'available', false, 'manual', NULL, NULL,
                            %s, 'github-app:installation_repositories',
                            'repository selected', CURRENT_TIMESTAMP
                        )
                        """,
                        (
                            repository_id,
                            installation_id,
                            (
                                None
                                if provider_id == 9001
                                else connection.execute(
                                    "SELECT CURRENT_TIMESTAMP"
                                ).fetchone()[0]
                            ),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO review_agent.github_app_repository_access_events (
                            repository_id, installation_id, event_kind,
                            access_state, enabled, trigger_mode, profile_key,
                            actor, reason, recorded_at
                        ) VALUES (
                            %s, %s, 'granted', 'available', false, 'manual',
                            NULL, 'github-app:installation_repositories',
                            'repository selected', CURRENT_TIMESTAMP
                        )
                        """,
                        (repository_id, installation_id),
                    )
                paused_id = repository_ids[9002]
                connection.execute(
                    """
                    INSERT INTO review_agent.github_app_repository_access_events (
                        repository_id, installation_id, event_kind, access_state,
                        enabled, trigger_mode, profile_key, actor, reason,
                        recorded_at
                    ) VALUES
                        (%s, %s, 'enabled', 'available', true, 'manual',
                         'default-standard', 'operator:owner',
                         'repository approved', CURRENT_TIMESTAMP),
                        (%s, %s, 'disabled', 'available', false, 'manual',
                         'default-standard', 'operator:owner',
                         'repository paused', CURRENT_TIMESTAMP)
                    """,
                    (paused_id, installation_id, paused_id, installation_id),
                )

            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection),
                    tuple(range(14, 36)),
                )
                classified = connection.execute(
                    """
                    SELECT repository.provider_repository_id,
                           access.automatic_activation_blocked
                    FROM review_agent.github_app_repository_access AS access
                    JOIN review_agent.repositories AS repository
                      ON repository.id = access.repository_id
                    ORDER BY repository.provider_repository_id
                    """
                ).fetchall()
                installation = github_app.get_installation_by_provider_id(
                    connection, 7001
                )
                github_app.set_repository_activation_policy(
                    connection,
                    installation_id=installation.id,
                    policy=github_app.RepositoryActivationPolicy.AUTOMATIC,
                    actor="operator:owner",
                    reason="approved organization-managed reviews",
                )
                activated = github_app.enable_automatic_repository(
                    connection,
                    provider_installation_id=7001,
                    provider_repository_id=9001,
                    full_name="Example-Org/untouched",
                    profile_key="default-standard",
                    actor="github-app:review-delivery",
                    reason="verified exact repository access",
                )
                with self.assertRaises(github_app.GitHubAppRepositoryUnauthorized):
                    github_app.enable_automatic_repository(
                        connection,
                        provider_installation_id=7001,
                        provider_repository_id=9002,
                        full_name="Example-Org/paused",
                        profile_key="default-standard",
                        actor="github-app:review-delivery",
                        reason="must preserve the operator pause",
                    )

        self.assertEqual(classified, [(9001, False), (9002, True)])
        self.assertTrue(activated.enabled)
        self.assertEqual(activated.trigger_mode, github_app.TriggerMode.AUTOMATIC)

    def test_purpose_migration_preserves_existing_code_records(self) -> None:
        purpose_tables = ("review_subjects", "review_runs", "finding_identities", "finding_occurrences", "publications")
        tables = (*purpose_tables, "finding_decisions", "pull_request_finding_references")
        with tempfile.TemporaryDirectory() as temp:
            previous = Path(temp)
            for source in MIGRATIONS.glob("*.sql"):
                if int(source.name[:3]) < 29:
                    shutil.copy2(source, previous / source.name)
            with psycopg.connect(DSN) as connection:
                runner.apply_migrations(connection, directory=previous)
                connection.execute("""
                    INSERT INTO review_agent.repositories (
                        provider, provider_repository_id, owner, name, full_name,
                        created_at, updated_at
                    ) VALUES ('github', 912, 'team', 'service', 'team/service', now(), now());
                    INSERT INTO review_agent.pull_requests (repository_id, number, created_at)
                    VALUES (1, 41, now());
                    INSERT INTO review_agent.review_subjects (
                        pull_request_id, base_sha, head_sha, policy_revision,
                        resolved_config_schema_version, resolved_config, resolved_config_hash, created_at
                    ) VALUES (1, repeat('b',40), repeat('a',40), 'profile@1', 1,
                              '{"profile":"default-standard"}', repeat('c',64), now());
                    INSERT INTO review_agent.review_runs (
                        pull_request_id, review_subject_id, request_key, status, phase,
                        started_at, last_heartbeat_at, completed_at, findings_count
                    ) VALUES (1, 1, 'existing:review', 'completed', 'posted', now(), now(), now(), 1);
                    INSERT INTO review_agent.review_runs (
                        pull_request_id, review_subject_id, request_key, status, phase,
                        started_at, last_heartbeat_at, completed_at, failure_code,
                        failure_status_comment_id, failure_status_posted_at,
                        failure_status_delivery_status, failure_status_delivery_completed_at
                    ) VALUES (1, 1, 'existing:failure', 'failed', 'failed', now(), now(), now(),
                              'review_failed', 8801, now(), 'posted', now());
                    INSERT INTO review_agent.finding_identities (
                        repository_id, fingerprint, rule_id, path, anchor, first_seen_at, last_seen_at
                    ) VALUES (1, repeat('f',64), 'correctness.rule', 'src/app.py', 'stable', now(), now());
                    INSERT INTO review_agent.finding_occurrences (
                        review_run_id, pull_request_id, repository_id, finding_id, line,
                        title, severity, category, publication_score, confidence, context_hash,
                        evidence, disproof_checks, impact, smallest_fix, observed_at
                    ) VALUES (1, 1, 1, 1, 12, 'State can be lost', 'High', 'correctness', 8, 0.95,
                              repeat('b',64), 'Lost update', 'Checked callers', 'Incomplete state',
                              'Persist atomically', now());
                    INSERT INTO review_agent.finding_decisions (
                        finding_id, finding_occurrence_id, decision, reason, actor,
                        context_hash, created_at, expires_at
                    ) VALUES (1, 1, 'accepted_risk', 'Reviewed tradeoff', 'maintainer',
                              repeat('b',64), now(), now() + interval '30 days');
                    INSERT INTO review_agent.pull_request_finding_references (
                        pull_request_id, repository_id, finding_id, local_reference, first_assigned_at
                    ) VALUES (1, 1, 1, 'F1', now());
                    INSERT INTO review_agent.publications (
                        pull_request_id, review_run_id, review_number, publication_key,
                        rendered_markdown, rendered_blocks_schema_version, rendered_blocks,
                        rendered_hash, status, generated_at, posting_started_at, posted_at,
                        delivery_completed_at
                    ) VALUES (1, 1, 1, 'sha256:' || repeat('d',64), 'Existing review', 1,
                              '[]', repeat('e',64), 'posted', now(), now(), now(), now());
                """)
                before = {
                    table: connection.execute(
                        psycopg.sql.SQL("SELECT to_jsonb(record) FROM review_agent.{} record ORDER BY id").format(
                            psycopg.sql.Identifier(table)
                        )
                    ).fetchall() for table in tables
                }
            with psycopg.connect(DSN) as connection:
                self.assertEqual(runner.apply_migrations(connection), tuple(range(29, 36)))
                for table in tables:
                    rows = connection.execute(
                        psycopg.sql.SQL("SELECT to_jsonb(record) - 'purpose', to_jsonb(record)->>'purpose' FROM review_agent.{} record ORDER BY id").format(
                            psycopg.sql.Identifier(table)
                        )
                    ).fetchall()
                    self.assertEqual([(row[0],) for row in rows], before[table])
                    if table in purpose_tables:
                        self.assertTrue(all(row[1] == "code" for row in rows))
                self.assertEqual(connection.execute(
                    "SELECT finding_id, local_reference FROM review_agent.pull_request_finding_references"
                ).fetchall(), [(1, "F1")])
                with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    with connection.transaction():
                        connection.execute("UPDATE review_agent.review_runs SET purpose = 'documentation' WHERE id = 1")
                with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    with connection.transaction():
                        connection.execute("UPDATE review_agent.publications SET purpose = 'documentation' WHERE id = 1")

    def test_rejects_an_applied_migration_whose_source_changed(self) -> None:
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "001_initial.sql"
            shutil.copy2(MIGRATIONS / "001_initial.sql", target)
            target.write_text(
                target.read_text(encoding="utf-8") + "\n-- drift\n",
                encoding="utf-8",
            )
            with psycopg.connect(DSN) as connection:
                with self.assertRaisesRegex(
                    runner.MigrationError, "checksum mismatch for 001_initial.sql"
                ):
                    runner.apply_migrations(connection, directory=directory)

        with psycopg.connect(DSN) as connection:
            count = connection.execute(
                "SELECT count(*) FROM review_agent.schema_migrations"
            ).fetchone()
        self.assertEqual(count, (35,))

    def test_previous_image_accepts_a_database_with_newer_migrations(self) -> None:
        with (
            tempfile.TemporaryDirectory() as current_temp,
            tempfile.TemporaryDirectory() as previous_temp,
        ):
            current = Path(current_temp)
            previous = Path(previous_temp)
            shutil.copy2(MIGRATIONS / "001_initial.sql", current / "001_initial.sql")
            shutil.copy2(MIGRATIONS / "001_initial.sql", previous / "001_initial.sql")
            (current / "002_newer.sql").write_text(
                "CREATE TABLE review_agent.newer_probe (id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )

            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection, directory=current), (1, 2)
                )
            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection, directory=previous), ()
                )

    def test_rejects_a_connection_with_an_existing_transaction(self) -> None:
        with psycopg.connect(DSN) as connection:
            connection.execute("SELECT 1")
            with self.assertRaisesRegex(
                runner.MigrationError, "requires an idle PostgreSQL connection"
            ):
                runner.apply_migrations(connection)
            connection.rollback()

    def test_inspection_reports_a_concurrent_migration_holder(self) -> None:
        with psycopg.connect(DSN) as lock_holder:
            with lock_holder.transaction():
                lock_holder.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (runner._MIGRATION_LOCK_KEY,),
                )
                with psycopg.connect(DSN) as connection:
                    with self.assertRaisesRegex(
                        runner.MigrationError,
                        "PostgreSQL migrations are currently running",
                    ):
                        runner.inspect_migrations(connection)

    def test_failed_migration_rolls_back_ddl_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "001_base.sql").write_text(
                "CREATE TABLE review_agent.rollback_probe (id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )
            (directory / "002_broken.sql").write_text(
                "CREATE TABLE review_agent.never_committed (id integer);\n"
                "SELECT review_agent.missing_function();\n",
                encoding="utf-8",
            )
            with psycopg.connect(DSN) as connection:
                with self.assertRaisesRegex(
                    runner.MigrationError, "failed to apply 002_broken.sql"
                ):
                    runner.apply_migrations(connection, directory=directory)

        with psycopg.connect(DSN) as connection:
            namespace = connection.execute(
                "SELECT to_regnamespace('review_agent')::text"
            ).fetchone()
        self.assertEqual(namespace, (None,))

    def test_concurrent_runners_serialize_and_apply_each_version_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "001_concurrent.sql").write_text(
                "SELECT pg_sleep(0.5);\n"
                "CREATE TABLE review_agent.concurrent_probe (id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )

            ready = Barrier(2, timeout=10)

            def apply_once(_: int) -> tuple[int, ...]:
                with psycopg.connect(DSN) as connection:
                    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                    ready.wait()
                    return runner.apply_migrations(connection, directory=directory)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(apply_once, range(2)))

        self.assertCountEqual(results, [(1,), ()])
        with psycopg.connect(DSN) as connection:
            ledger = connection.execute(
                "SELECT version, count(*) FROM review_agent.schema_migrations GROUP BY version"
            ).fetchall()
        self.assertEqual(ledger, [(1, 1)])
