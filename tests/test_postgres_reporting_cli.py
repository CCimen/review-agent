from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    operator_application,
    review_finding_application,
    review_run_application,
)
from review_agent_tools.domain.finding import FindingInput  # noqa: E402
from review_agent_tools.domain.publication import (  # noqa: E402
    PublicationFindingInput,
    PublicationFindingOutcome,
    PublicationPartInput,
    PublicationPartType,
    resolve_publication_plan,
)
from review_agent_tools.domain.review import DiffState, ReviewPhase  # noqa: E402
from review_agent_tools.postgres import (  # noqa: E402
    publications,
    quality_reporting,
    registry,
    reporting,
    review_runs,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLOperatorReportingTests(unittest.TestCase):
    repository = "example-org/example-repository"
    head_sha = "a" * 40

    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    def register_repository(self) -> None:
        with self.runtime.transaction() as connection:
            registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=930,
                    full_name=self.repository,
                ),
            )

    @staticmethod
    def finding(**overrides: object) -> FindingInput:
        item = FindingInput(
            rule_id="correctness.boolean-default",
            category="correctness",
            path="src/flags.py",
            line=2,
            symbol="safe",
            anchor="safe default",
            title="Safe mode defaults to disabled",
            severity="High",
            publication_score=9,
            confidence=0.95,
            evidence="The changed default is false.",
            disproof_checks="Checked all callers.",
            impact="Requests can run without the expected guard.",
            smallest_fix="Restore the true default.",
            introduced_by_diff=True,
        )
        return replace(item, **overrides)

    def start(
        self,
        *,
        pr_number: int,
        request_suffix: str,
        head_sha: str | None = None,
        paths: tuple[str, ...] = ("src/flags.py",),
        policy_revision: str = "profile@1",
        resolved_config: dict[str, object] | None = None,
    ) -> review_runs.StartedRun:
        selected_head = head_sha or self.head_sha
        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=930,
                repository=self.repository,
                pr_number=pr_number,
                base_sha="b" * 40,
                head_sha=selected_head,
                policy_revision=policy_revision,
                resolved_config_schema_version=(
                    2 if resolved_config is not None else 1
                ),
                resolved_config=(
                    resolved_config
                    if resolved_config is not None
                    else {"profile": "sundsvall-standard"}
                ),
                request_key=f"github:issue-comment:{request_suffix}",
            ),
        )
        assert isinstance(result, review_runs.StartedRun)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=result.run.id,
            files=tuple(
                review_run_application.PostgresChangedFile(
                    path=path, change_status="modified"
                )
                for path in paths
            ),
            changed_files_reported=len(paths),
            registration_complete=True,
        )
        return result

    def record_finding(
        self,
        run: review_runs.StartedRun,
        *,
        findings: tuple[FindingInput, ...] | None = None,
        context_hash: str = "c" * 40,
        head_sha: str | None = None,
    ) -> review_finding_application.PostgresFindingBatch:
        selected = findings if findings is not None else (self.finding(),)
        return review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=run.run.id,
            head_sha=head_sha or self.head_sha,
            findings=selected,
            changed_files=tuple(
                review_finding_application.ChangedFile(
                    path=path,
                    context_hash=context_hash,
                    context_hash_source="blob",
                )
                for path in dict.fromkeys(item.path for item in selected)
            ),
        )

    def publish_run(
        self,
        run: review_runs.StartedRun,
        batch: review_finding_application.PostgresFindingBatch,
        *,
        key_character: str,
    ) -> publications.Publication:
        for phase in (
            ReviewPhase.FETCHING_PR,
            ReviewPhase.COLLECTING_DIFF,
            ReviewPhase.REVIEWING,
            ReviewPhase.RENDERING,
        ):
            with self.runtime.transaction() as connection:
                review_runs.advance_phase(connection, run.run.id, phase)
        publication_key = "sha256:" + (key_character * 64)
        plan = resolve_publication_plan(
            publication_key=publication_key,
            rendered_markdown="## Review\n\nExact persisted review.\n",
            rendered_blocks_schema_version=1,
            rendered_blocks=(
                {"kind": "header", "markdown": "## Review"},
                {"kind": "finding", "markdown": "Exact persisted review."},
            ),
            parts=(
                PublicationPartInput(
                    part_type=PublicationPartType.SUMMARY,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "Exact persisted review.\n\n"
                            "<!-- review-agent:canonical publication="
                            + publication_key
                            + " part=1/1 -->"
                        )
                    },
                ),
            ),
            findings=tuple(
                PublicationFindingInput(
                    finding_id=int(item.finding_id),
                    source_finding_occurrence_id=int(item.occurrence_id),
                    source_review_run_id=int(batch.run_id),
                    local_reference=item.local_reference,
                    outcome=PublicationFindingOutcome.CURRENT,
                )
                for item in batch.items
            ),
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run.run.id, plan=plan
            )
        with self.runtime.transaction() as connection:
            claim = publications.claim_publication(connection, prepared.id)
        assert claim.publication.posting_started_at is not None
        with self.runtime.transaction() as connection:
            publications.acknowledge_part(
                connection,
                publication_id=prepared.id,
                part_type=PublicationPartType.SUMMARY,
                part_number=1,
                external_id=900 + int(run.run.id),
                posting_started_at=claim.publication.posting_started_at,
            )
            posted = publications.complete_publication(
                connection,
                publication_id=prepared.id,
                posting_started_at=claim.publication.posting_started_at,
            )
            review_runs.advance_phase(
                connection, run.run.id, ReviewPhase.PUBLISHING
            )
            review_runs.complete_run(
                connection, run.run.id, findings_count=len(batch.items)
            )
        return posted

    def test_live_context_filters_before_limit_and_resolves_repeat_suppression(
        self,
    ) -> None:
        prior = self.start(pr_number=60, request_suffix="4401")
        prior_batch = self.record_finding(
            prior,
            findings=(
                self.finding(),
                self.finding(
                    rule_id="reliability.retry-contract",
                    anchor="retry contract",
                    title="Retry contract can lose a request",
                ),
            ),
        )
        operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=prior_batch.items[0].fingerprint,
                decision="false_positive",
                reason="The reviewed context proves this is intentional.",
                actor="operator@example.org",
                occurrence_id=int(prior_batch.items[0].occurrence_id),
                expires_days=45,
            ),
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.start(
            pr_number=60,
            request_suffix="4402",
            head_sha="d" * 40,
        )

        unrelated = tuple(
            self.finding(path=f"src/unrelated_{index:03d}.py")
            for index in range(200)
        )
        self.record_finding(
            self.start(
                pr_number=61,
                request_suffix="4403",
                paths=tuple(item.path for item in unrelated),
            ),
            findings=unrelated,
        )

        context = review_finding_application.load_live_context(
            self.runtime,
            review_finding_application.FindingContextQuery(
                repository=self.repository,
                paths=("src/flags.py",),
                pr_number=60,
            ),
        )

        self.assertEqual(
            {item["fingerprint"] for item in context["recent_findings"]},
            {item.fingerprint for item in prior_batch.items},
        )
        self.assertEqual(
            [item["fingerprint"] for item in context["historical_suppressions"]],
            [prior_batch.items[0].fingerprint],
        )
        self.assertEqual(
            [item["fingerprint"] for item in context["repeat_review_findings"]],
            [prior_batch.items[1].fingerprint],
        )

        repository_context = review_finding_application.load_live_context(
            self.runtime,
            review_finding_application.FindingContextQuery(
                repository=self.repository,
                paths=(),
            ),
        )
        self.assertEqual(len(repository_context["recent_findings"]), 30)
        self.assertEqual(
            [
                item["path"]
                for item in repository_context["recent_findings"]
                if not str(item["path"]).startswith("src/unrelated_")
            ],
            [],
        )

    def test_visible_finding_limit_is_applied_after_suppression(self) -> None:
        visible_findings = (
            self.finding(path="src/visible_one.py"),
            self.finding(path="src/visible_two.py"),
        )
        visible = self.record_finding(
            self.start(
                pr_number=62,
                request_suffix="4411",
                paths=tuple(item.path for item in visible_findings),
            ),
            findings=visible_findings,
        )
        suppressed_findings = (
            self.finding(path="src/suppressed_one.py"),
            self.finding(path="src/suppressed_two.py"),
        )
        suppressed = self.record_finding(
            self.start(
                pr_number=63,
                request_suffix="4412",
                paths=tuple(item.path for item in suppressed_findings),
            ),
            findings=suppressed_findings,
        )
        for item in suppressed.items:
            operator_application.decide_finding(
                self.runtime,
                operator_application.OperatorDecisionRequest(
                    repository=self.repository,
                    fingerprint=item.fingerprint,
                    decision="false_positive",
                    reason="The reviewed context proves this is intentional.",
                    actor="operator@example.org",
                    occurrence_id=int(item.occurrence_id),
                    expires_days=45,
                ),
                now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
            )

        listed = operator_application.list_findings(
            self.runtime,
            repository=self.repository,
            limit=2,
            include_suppressed=False,
            now=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            {item.fingerprint for item in listed},
            {item.fingerprint for item in visible.items},
        )

    def test_finding_inspection_decision_stats_and_bounded_export(self) -> None:
        run = self.start(pr_number=31, request_suffix="4101")
        batch = self.record_finding(run)
        fingerprint = batch.items[0].fingerprint

        decision = operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=fingerprint[:12],
                decision="false_positive",
                reason="The platform guarantees this precondition.",
                actor="operator@example.org",
                occurrence_id=int(batch.items[0].occurrence_id),
                expires_days=45,
            ),
            now=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(decision.fingerprint, fingerprint)

        listed = operator_application.list_findings(
            self.runtime,
            repository=self.repository,
            limit=20,
            include_suppressed=True,
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0].suppressed)
        self.assertEqual(listed[0].latest_decision, decision.decision)

        shown = operator_application.show_finding(
            self.runtime,
            repository=self.repository,
            fingerprint=fingerprint[:12],
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(shown.finding.fingerprint, fingerprint)
        self.assertEqual(tuple(item.id for item in shown.decisions), (decision.id,))

        stats = operator_application.finding_stats(
            self.runtime,
            repository=self.repository,
            expiring_within_days=60,
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(stats.findings_total, 1)
        self.assertEqual(stats.active_suppressions, 1)
        self.assertEqual(stats.active_suppressions_nearing_expiry, 1)

        exported = operator_application.export_repository(
            self.runtime,
            repository=self.repository,
            row_limit=1,
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        payload = exported.to_json_obj()
        self.assertEqual(payload["repository"], self.repository)
        observations = payload["finding_observations"]
        decisions = payload["decisions"]
        self.assertIsInstance(observations, list)
        self.assertIsInstance(decisions, list)
        assert isinstance(observations, list)
        assert isinstance(decisions, list)
        self.assertEqual(len(observations), 1)
        self.assertEqual(len(decisions), 1)

        with self.runtime.transaction() as connection:
            scope = reporting.repository_scope(
                connection, repository=self.repository
            )
            with self.assertRaisesRegex(
                reporting.ReportingError, "repeatable-read"
            ):
                reporting.export_repository(
                    connection,
                    scope=scope,
                    row_limit=1,
                    now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
                )

        with self.assertRaises(operator_application.OperatorInputError):
            operator_application.export_repository(
                self.runtime,
                repository=self.repository,
                row_limit=0,
            )

    def test_run_reporting_and_stale_recovery_are_atomic(self) -> None:
        stale = self.start(pr_number=41, request_suffix="4201")
        completed = self.start(pr_number=42, request_suffix="4202")
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_runs "
                "SET started_at = %s, last_heartbeat_at = %s WHERE id = %s",
                (
                    datetime(2026, 8, 20, tzinfo=timezone.utc),
                    datetime(2026, 8, 20, tzinfo=timezone.utc),
                    stale.run.id,
                ),
            )
            for phase in (
                ReviewPhase.FETCHING_PR,
                ReviewPhase.COLLECTING_DIFF,
                ReviewPhase.REVIEWING,
                ReviewPhase.RENDERING,
                ReviewPhase.PUBLISHING,
            ):
                review_runs.advance_phase(connection, completed.run.id, phase)
            review_runs.complete_run(connection, completed.run.id, findings_count=2)

        result = operator_application.mark_stalled_runs(
            self.runtime,
            repository=self.repository,
            pr_number=41,
            older_than_minutes=30,
            now=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.runs[0].failure_code, "stale_timeout")

        listed = operator_application.list_runs(
            self.runtime,
            repository=self.repository,
            limit=20,
        )
        self.assertEqual({item.status for item in listed}, {"completed", "failed"})

        stats = operator_application.run_stats(
            self.runtime,
            repository=self.repository,
            days=30,
            stale_after_minutes=30,
            now=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(stats.total, 2)
        self.assertEqual(
            {item.value: item.count for item in stats.by_status},
            {"completed": 1, "failed": 1},
        )
        self.assertEqual(stats.average_findings_per_completed_run, 2.0)
        coverage = operator_application.coverage(self.runtime, run_id=int(stale.run.id))
        self.assertEqual(coverage.changed_files_registered, 1)
        self.assertTrue(coverage.coverage_hash.startswith("sha256:"))

    def test_failure_status_delivery_exhaustion_is_visible_to_operator(self) -> None:
        started = self.start(pr_number=43, request_suffix="4301")
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, started.run.id, failure_code="review_deliver_error"
            )
        for attempt in range(3):
            with self.runtime.transaction() as connection:
                claim = review_runs.claim_failure_status(
                    connection, run_id=started.run.id,
                    lease_owner=f"failed-publisher-{attempt}",
                    lease_duration=timedelta(minutes=1),
                )
                review_runs.retry_failure_status(
                    connection, run_id=started.run.id,
                    lease_owner=f"failed-publisher-{attempt}",
                    lease_generation=claim.target.delivery_lease_generation,
                    failure_code="github_unreachable", retry_delay=timedelta(0),
                )

        listed = operator_application.list_runs(
            self.runtime, repository=self.repository, limit=10, failed_only=True
        )

        report = next(item for item in listed if item.id == started.run.id)
        self.assertEqual(report.failure_status_delivery_status, "failed")
        self.assertEqual(report.failure_status_delivery_attempt_count, 3)
        self.assertEqual(report.failure_status_delivery_max_attempts, 3)
        self.assertEqual(
            report.failure_status_delivery_failure_code, "github_unreachable"
        )

        queued = operator_application.prepare_failure_status_queue(
            self.runtime,
            repository=self.repository,
            pr_number=43,
            older_than_minutes=60,
        )
        self.assertEqual(tuple(item.run_id for item in queued.targets), (started.run.id,))
        with self.runtime.transaction() as connection:
            recovered = review_runs.claim_failure_status(
                connection,
                run_id=started.run.id,
                lease_owner="operator-recovery",
                lease_duration=timedelta(minutes=1),
            )
        self.assertEqual(recovered.target.delivery_status, "posting")
        self.assertEqual(recovered.target.delivery_attempt_count, 1)

    def test_decision_target_modes_are_explicit_and_pr_scoped(self) -> None:
        first = self.start(pr_number=46, request_suffix="4251")
        first_batch = self.record_finding(first)
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, first.run.id, failure_code="test_terminal"
            )
        second = self.start(pr_number=47, request_suffix="4252")
        second_batch = self.record_finding(second)
        fingerprint = first_batch.items[0].fingerprint

        latest = operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=fingerprint,
                decision="resolved",
                reason="The latest occurrence is fixed.",
                actor="operator@example.org",
                latest=True,
            ),
            now=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            latest.occurrence_id, second_batch.items[0].occurrence_id
        )

        local = operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=fingerprint,
                decision="reopen",
                reason="The current PR occurrence needs another review.",
                actor="operator@example.org",
                pr_number=46,
                local_reference=first_batch.items[0].local_reference,
            ),
            now=datetime(2026, 8, 24, 12, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(local.occurrence_id, first_batch.items[0].occurrence_id)
        self.assertNotEqual(local.occurrence_id, latest.occurrence_id)

        with self.assertRaises(operator_application.OperatorInputError):
            operator_application.decide_finding(
                self.runtime,
                operator_application.OperatorDecisionRequest(
                    repository=self.repository,
                    fingerprint=fingerprint,
                    decision="resolved",
                    reason="Ambiguous target must fail before persistence.",
                    actor="operator@example.org",
                    occurrence_id=int(first_batch.items[0].occurrence_id),
                    latest=True,
                ),
                now=datetime(2026, 8, 24, 12, 2, tzinfo=timezone.utc),
            )

    def test_open_only_and_export_truncation_are_observable(self) -> None:
        run = self.start(pr_number=47, request_suffix="4261")
        batch = self.record_finding(
            run,
            findings=(
                self.finding(),
                self.finding(
                    rule_id="reliability.timeout-default",
                    anchor="timeout default",
                    title="Timeout remains disabled",
                ),
            ),
        )
        operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=batch.items[0].fingerprint,
                decision="false_positive",
                reason="The platform owns this precondition.",
                actor="operator@example.org",
                occurrence_id=int(batch.items[0].occurrence_id),
                expires_days=45,
            ),
            now=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )
        open_findings = operator_application.list_findings(
            self.runtime,
            repository=self.repository,
            limit=20,
            include_suppressed=False,
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            tuple(item.fingerprint for item in open_findings),
            (batch.items[1].fingerprint,),
        )

        exported = operator_application.export_repository(
            self.runtime,
            repository=self.repository,
            row_limit=1,
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        ).to_json_obj()
        self.assertEqual(exported["schema_version"], 17)
        self.assertEqual(exported["complete"], False)
        truncated = exported["truncated_tables"]
        self.assertIsInstance(truncated, list)
        assert isinstance(truncated, list)
        self.assertIn("findings", truncated)
        self.assertIn("finding_observations", truncated)

    def test_coach_result_uses_existing_postgres_owner(self) -> None:
        self.start(pr_number=51, request_suffix="4301")
        stored = operator_application.record_coach_run(
            self.runtime,
            repository=self.repository,
            source_event_set_id="sha256:" + ("1" * 64),
            source_snapshot_id="sha256:" + ("2" * 64),
            proposal_set_id="sha256:" + ("3" * 64),
            events_considered=0,
            artifact_dir="/private/coach/2026-08-24",
            candidates=(),
        )
        self.assertEqual(stored.decision, "no_change")
        self.assertEqual(stored.repository, self.repository)

    def test_quality_report_has_explicit_zero_denominators(self) -> None:
        self.register_repository()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        report = operator_application.quality_report(
            self.runtime,
            repository=self.repository,
            days=30,
            now=now,
        )

        self.assertEqual(report.repository, self.repository)
        self.assertEqual(report.window_days, 30)
        self.assertEqual(report.completed_reviews, 0)
        self.assertEqual(report.published_findings, 0)
        self.assertEqual(report.complete_coverage_reviews, 0)
        self.assertEqual(report.false_positive_signals.count, 0)
        self.assertEqual(report.false_positive_signals.denominator, 0)
        self.assertEqual(
            report.false_positive_signals.denominator_name,
            "published_findings",
        )
        self.assertEqual(report.scope_confusion_signals.denominator, 0)
        self.assertEqual(report.missed_issue_signals.denominator, 0)
        self.assertIsNone(report.oldest_triage_backlog_seconds)
        self.assertEqual(report.cohorts, ())
        with self.assertRaises(reporting.RepositoryNotFound):
            operator_application.quality_report(
                self.runtime,
                repository="example-org/not-registered",
                days=30,
                now=now,
            )

    def test_quality_cli_renders_json_and_markdown_without_accuracy_claims(
        self,
    ) -> None:
        self.register_repository()
        environment = {**os.environ, "REVIEW_AGENT_DATABASE_URL": DSN}
        outputs: dict[str, str] = {}
        for output_format in ("json", "markdown"):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "review_agent_memory.py"),
                    "quality",
                    "--days",
                    "30",
                    "--repo",
                    self.repository,
                    f"--{output_format}",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs[output_format] = completed.stdout

        payload = json.loads(outputs["json"])
        self.assertEqual(payload["completed_reviews"], 0)
        self.assertEqual(
            payload["false_positive_signals"]["denominator_name"],
            "published_findings",
        )
        self.assertIn("# Review quality", outputs["markdown"])
        self.assertIn("False-positive decisions", outputs["markdown"])
        for forbidden in ("accuracy", "precision", "recall"):
            self.assertNotIn(forbidden, outputs["markdown"].casefold())

    def test_quality_report_uses_explicit_signals_and_stable_cohorts(self) -> None:
        with self.runtime.transaction() as connection:
            clock = connection.execute("SELECT statement_timestamp()").fetchone()
        assert clock is not None and isinstance(clock[0], datetime)
        now = clock[0] + timedelta(minutes=1)
        contract = {
            "sha256": "4" * 64,
            "model_provider": "openai-codex",
            "model": "gpt-5.6-sol",
        }
        resolved_config: dict[str, object] = {
            "profile": "sundsvall-standard",
            "review_contract": contract,
        }
        first = self.start(
            pr_number=71,
            request_suffix="4501",
            paths=("src/flags.py", "src/missing.py"),
            resolved_config=resolved_config,
        )
        first_batch = self.record_finding(first)
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.finding_occurrences "
                "SET observed_at = %s WHERE review_run_id = %s",
                (now - timedelta(days=2), first.run.id),
            )
        review_run_application.record_postgres_diff_observation(
            self.runtime,
            run_id=first.run.id,
            paths=("src/flags.py",),
            state=DiffState.COMPLETE,
        )
        review_run_application.record_postgres_diff_observation(
            self.runtime,
            run_id=first.run.id,
            paths=("src/missing.py",),
            state=DiffState.UNAVAILABLE,
            unavailable_reason="patch_unavailable",
        )
        first_publication = self.publish_run(
            first, first_batch, key_character="5"
        )
        operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=first_batch.items[0].fingerprint,
                decision="false_positive",
                reason="The exact context has a platform-owned guard.",
                actor="github:operator",
                occurrence_id=int(first_batch.items[0].occurrence_id),
                expires_days=45,
            ),
            now=now - timedelta(days=1, hours=2),
        )

        second_findings = (
            self.finding(),
            self.finding(
                rule_id="reliability.retry-contract",
                path="src/retry.py",
                anchor="retry contract",
                title="Retry contract can lose a request",
            ),
        )
        second = self.start(
            pr_number=72,
            request_suffix="4502",
            head_sha="e" * 40,
            paths=tuple(item.path for item in second_findings),
            policy_revision="profile@2",
            resolved_config=resolved_config,
        )
        second_batch = self.record_finding(
            second,
            findings=second_findings,
            context_hash="e" * 40,
            head_sha="e" * 40,
        )
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.finding_occurrences "
                "SET observed_at = %s WHERE review_run_id = %s",
                (now - timedelta(hours=2), second.run.id),
            )
        review_run_application.record_postgres_diff_observation(
            self.runtime,
            run_id=second.run.id,
            paths=tuple(item.path for item in second_findings),
            state=DiffState.COMPLETE,
        )
        second_publication = self.publish_run(
            second, second_batch, key_character="6"
        )
        operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.repository,
                fingerprint=second_batch.items[1].fingerprint,
                decision="false_positive",
                reason="The exact retry context is covered by the queue fence.",
                actor="github:operator",
                occurrence_id=int(second_batch.items[1].occurrence_id),
                expires_days=45,
            ),
            now=now - timedelta(hours=1),
        )

        with self.runtime.transaction() as connection:
            scope_feedback_id = int(
                connection.execute(
                    """
                    INSERT INTO review_agent.review_quality_feedback (
                        pull_request_id, publication_id, local_reference,
                        category, reason, actor_user_id, created_at
                    ) VALUES (%s, %s, %s, 'scope_confusion', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        second.run.pull_request_id,
                        second_publication.id,
                        second_batch.items[0].local_reference,
                        "This finding is outside the pull request scope.",
                        "developer-1",
                        now - timedelta(hours=4),
                    ),
                ).fetchone()[0]
            )
            missed_rows = connection.execute(
                """
                INSERT INTO review_agent.review_quality_feedback (
                    pull_request_id, publication_id, category, reason,
                    actor_user_id, created_at
                ) VALUES
                    (%s, %s, 'missed_issue', %s, %s, %s),
                    (%s, %s, 'missed_issue', %s, %s, %s)
                RETURNING id
                """,
                (
                    first.run.pull_request_id,
                    first_publication.id,
                    "The review missed unavailable source coverage.",
                    "developer-2",
                    now - timedelta(days=45),
                    second.run.pull_request_id,
                    second_publication.id,
                    "The review missed the repository decision.",
                    "developer-3",
                    now - timedelta(hours=3),
                ),
            ).fetchall()
        self.assertGreater(scope_feedback_id, 0)
        actionable_feedback_id = int(missed_rows[1][0])
        operator_application.triage_review_feedback(
            self.runtime,
            feedback_id=actionable_feedback_id,
            status="actionable",
            stable_key="review.missed-repository-decision",
            target_owner="repository_decision",
            evidence_reference="https://github.com/example-org/example-repository/issues/9",
            path="src/flags.py",
            category="correctness",
            actor="github:operator",
            reason="A focused regression confirms the missing decision context.",
            now=now - timedelta(hours=2),
        )

        report = operator_application.quality_report(
            self.runtime,
            repository=self.repository,
            days=30,
            now=now,
        )

        self.assertEqual(report.completed_reviews, 2)
        self.assertEqual(report.published_findings, 3)
        self.assertEqual(report.complete_coverage_reviews, 1)
        self.assertEqual(report.false_positive_signals.count, 2)
        self.assertEqual(report.false_positive_signals.denominator, 3)
        self.assertEqual(report.scope_confusion_signals.count, 1)
        self.assertEqual(report.scope_confusion_signals.denominator, 2)
        self.assertEqual(report.missed_issue_signals.count, 1)
        self.assertEqual(report.missed_issue_signals.denominator, 2)
        self.assertEqual(report.triage_backlog, 1)
        self.assertEqual(report.oldest_triage_backlog_seconds, 45 * 24 * 60 * 60)
        self.assertEqual(
            tuple(
                (item.value, item.count)
                for item in report.actionable_missed_issues_by_target_owner
            ),
            (("repository_decision", 1),),
        )
        self.assertEqual(report.active_suppressions, 1)
        self.assertEqual(report.suppressions_invalidated_by_context, 1)
        self.assertEqual(report.repeat_findings_after_suppressive_decision, 1)
        self.assertEqual(
            {(item.value, item.count) for item in report.noisy_rule_ids},
            {
                ("correctness.boolean-default", 1),
                ("reliability.retry-contract", 1),
            },
        )
        self.assertEqual(
            tuple(
                (item.value, item.count)
                for item in report.coverage_failure_codes
            ),
            (("patch_unavailable", 1),),
        )
        self.assertEqual(len(report.cohorts), 2)
        self.assertEqual(
            {item.policy_revision for item in report.cohorts},
            {"profile@1", "profile@2"},
        )
        self.assertTrue(
            all(item.review_contract_hash == "4" * 64 for item in report.cohorts)
        )
        self.assertTrue(
            all(item.model_provider == "openai-codex" for item in report.cohorts)
        )
        markdown = quality_reporting.render_markdown(report)
        self.assertIn("3 published findings", markdown)
        self.assertIn("profile@2", markdown)
        self.assertIn("Current governed state", markdown)

    def test_publication_listing_and_verification_export_reuse_durable_plan(
        self,
    ) -> None:
        run = self.start(pr_number=61, request_suffix="4401")
        batch = self.record_finding(run)
        for phase in (
            ReviewPhase.FETCHING_PR,
            ReviewPhase.COLLECTING_DIFF,
            ReviewPhase.REVIEWING,
            ReviewPhase.RENDERING,
        ):
            with self.runtime.transaction() as connection:
                review_runs.advance_phase(connection, run.run.id, phase)
        publication_key = "sha256:" + ("d" * 64)
        plan = resolve_publication_plan(
            publication_key=publication_key,
            rendered_markdown="## Review\n\nExact persisted review.\n",
            rendered_blocks_schema_version=1,
            rendered_blocks=(
                {"kind": "header", "markdown": "## Review"},
                {"kind": "finding", "markdown": "Exact persisted review."},
            ),
            parts=(
                PublicationPartInput(
                    part_type=PublicationPartType.SUMMARY,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "Exact persisted review.\n\n"
                            "<!-- review-agent:canonical publication="
                            + publication_key
                            + " part=1/1 -->"
                        )
                    },
                ),
            ),
            findings=(
                PublicationFindingInput(
                    finding_id=int(batch.items[0].finding_id),
                    source_finding_occurrence_id=int(batch.items[0].occurrence_id),
                    source_review_run_id=int(batch.run_id),
                    local_reference=batch.items[0].local_reference,
                    outcome=PublicationFindingOutcome.CURRENT,
                ),
            ),
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run.run.id, plan=plan
            )
        with self.runtime.transaction() as connection:
            claim = publications.claim_publication(connection, prepared.id)
        assert claim.publication.posting_started_at is not None
        with self.runtime.transaction() as connection:
            publications.acknowledge_part(
                connection,
                publication_id=prepared.id,
                part_type=PublicationPartType.SUMMARY,
                part_number=1,
                external_id=901,
                posting_started_at=claim.publication.posting_started_at,
            )
            publications.complete_publication(
                connection,
                publication_id=prepared.id,
                posting_started_at=claim.publication.posting_started_at,
            )
            review_runs.advance_phase(
                connection, run.run.id, ReviewPhase.PUBLISHING
            )
            review_runs.complete_run(connection, run.run.id, findings_count=1)

        listed = operator_application.list_publications(
            self.runtime,
            repository=self.repository,
            pr_number=61,
            limit=10,
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].comment_ids, (901,))
        self.assertEqual(listed[0].status, "posted")

        source = operator_application.verification_export_source(
            self.runtime, run_id=int(run.run.id)
        )
        self.assertEqual(source["source_schema_version"], 1)
        run_source = source["run"]
        findings_source = source["current_findings"]
        self.assertIsInstance(run_source, dict)
        self.assertIsInstance(findings_source, list)
        assert isinstance(run_source, dict)
        assert isinstance(findings_source, list)
        self.assertEqual(run_source["status"], "generated")
        self.assertEqual(len(findings_source), 1)


if __name__ == "__main__":
    unittest.main()
