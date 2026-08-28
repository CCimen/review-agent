"""Bounded, denominator-based review quality reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from ..domain.finding import SUPPRESSIVE_DECISION_KINDS
from . import reporting as base_reporting


class QualityReportingError(ValueError):
    """A quality report cannot be built from the requested scope."""


@dataclass(frozen=True, slots=True)
class SignalCount:
    count: int
    denominator: int
    denominator_name: str


@dataclass(frozen=True, slots=True)
class RankedCount:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class QualityCohort:
    repository: str
    profile: str
    review_contract_hash: str
    model_provider: str
    model: str
    policy_revision: str
    completed_reviews: int


@dataclass(frozen=True, slots=True)
class QualityReport:
    schema_version: int
    window_started_at: datetime
    window_ended_at: datetime
    window_days: int
    repository: str | None
    completed_reviews: int
    published_findings: int
    complete_coverage_reviews: int
    false_positive_signals: SignalCount
    scope_confusion_signals: SignalCount
    missed_issue_signals: SignalCount
    triage_backlog: int
    oldest_triage_backlog_seconds: int | None
    actionable_missed_issues_by_target_owner: tuple[RankedCount, ...]
    active_suppressions: int
    suppressions_invalidated_by_context: int
    repeat_findings_after_suppressive_decision: int
    noisy_rule_ids: tuple[RankedCount, ...]
    coverage_failure_codes: tuple[RankedCount, ...]
    cohorts: tuple[QualityCohort, ...]


_RANKING_LIMIT = 10
_SUPPRESSIVE_DECISIONS = tuple(
    item.value for item in SUPPRESSIVE_DECISION_KINDS
)


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise QualityReportingError(
            "quality reporting requires an active transaction"
        )


def build_report(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    window_started_at: datetime,
    window_ended_at: datetime,
    window_days: int,
) -> QualityReport:
    """Build one read-only report from explicit persisted signals."""
    _require_transaction(connection)
    row = connection.execute(
        """
        WITH scoped_repositories AS (
            SELECT id
            FROM review_agent.repositories
            WHERE %s::text IS NULL OR lower(full_name) = lower(%s::text)
        ), completed_runs AS (
            SELECT run.id, run.changed_files_reported,
                   run.changed_file_registration_complete
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN scoped_repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE run.status = 'completed'
              AND run.completed_at >= %s
              AND run.completed_at < %s
        ), posted_publications AS (
            SELECT publication.id
            FROM review_agent.publications AS publication
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = publication.pull_request_id
            JOIN scoped_repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE publication.status = 'posted'
              AND publication.posted_at >= %s
              AND publication.posted_at < %s
        )
        SELECT
            (SELECT EXISTS(SELECT 1 FROM scoped_repositories)),
            (SELECT count(*)::integer FROM completed_runs),
            (
                SELECT count(*)::integer
                FROM review_agent.publication_findings AS finding
                JOIN posted_publications AS publication
                  ON publication.id = finding.publication_id
                WHERE finding.outcome = 'current'
            ),
            (
                SELECT count(*)::integer
                FROM completed_runs AS run
                WHERE run.changed_files_reported IS NOT NULL
                  AND run.changed_file_registration_complete
                  AND run.changed_files_reported = (
                      SELECT count(*)::integer
                      FROM review_agent.review_run_files AS file
                      WHERE file.review_run_id = run.id AND file.is_changed_path
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM review_agent.review_run_files AS file
                      WHERE file.review_run_id = run.id
                        AND file.is_changed_path
                        AND file.diff_state <> 'complete'
                  )
            ),
            (
                SELECT count(*)::integer
                FROM review_agent.finding_decisions AS decision
                JOIN review_agent.finding_identities AS identity
                  ON identity.id = decision.finding_id
                JOIN scoped_repositories AS repository
                  ON repository.id = identity.repository_id
                WHERE decision.decision = 'false_positive'
                  AND decision.created_at >= %s
                  AND decision.created_at < %s
            ),
            (
                SELECT count(*)::integer
                FROM review_agent.review_quality_feedback AS feedback
                JOIN review_agent.pull_requests AS pull_request
                  ON pull_request.id = feedback.pull_request_id
                JOIN scoped_repositories AS repository
                  ON repository.id = pull_request.repository_id
                WHERE feedback.category = 'scope_confusion'
                  AND feedback.created_at >= %s
                  AND feedback.created_at < %s
            ),
            (
                SELECT count(*)::integer
                FROM review_agent.review_quality_feedback AS feedback
                JOIN review_agent.pull_requests AS pull_request
                  ON pull_request.id = feedback.pull_request_id
                JOIN scoped_repositories AS repository
                  ON repository.id = pull_request.repository_id
                WHERE feedback.category = 'missed_issue'
                  AND feedback.created_at >= %s
                  AND feedback.created_at < %s
            )
        """,
        (
            repository,
            repository,
            window_started_at,
            window_ended_at,
            window_started_at,
            window_ended_at,
            window_started_at,
            window_ended_at,
            window_started_at,
            window_ended_at,
            window_started_at,
            window_ended_at,
        ),
    ).fetchone()
    if row is None:
        raise QualityReportingError("quality totals could not be computed")
    if repository is not None and not bool(row[0]):
        raise base_reporting.RepositoryNotFound(
            "repository is not registered"
        )
    completed_reviews = int(row[1])
    published_findings = int(row[2])
    triage_rows = connection.execute(
        """
        WITH scoped_feedback AS (
            SELECT feedback.id, feedback.created_at
            FROM review_agent.review_quality_feedback AS feedback
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = feedback.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE feedback.category = 'missed_issue'
              AND feedback.created_at < %s
              AND (
                  %s::text IS NULL
                  OR lower(repository.full_name) = lower(%s::text)
              )
        ), latest_triage AS (
            SELECT DISTINCT ON (stored.feedback_id)
                   stored.feedback_id, stored.status, stored.target_owner
            FROM review_agent.review_quality_feedback_triage AS stored
            JOIN scoped_feedback AS feedback
              ON feedback.id = stored.feedback_id
            WHERE stored.created_at < %s
            ORDER BY stored.feedback_id, stored.id DESC
        )
        SELECT COALESCE(latest.status, 'pending') AS status,
               latest.target_owner,
               count(*)::integer,
               extract(
                   epoch FROM %s - min(feedback.created_at)
               )::bigint AS oldest_seconds
        FROM scoped_feedback AS feedback
        LEFT JOIN latest_triage AS latest ON latest.feedback_id = feedback.id
        GROUP BY status, latest.target_owner
        ORDER BY status, latest.target_owner
        """,
        (
            window_ended_at,
            repository,
            repository,
            window_ended_at,
            window_ended_at,
        ),
    ).fetchall()
    triage_backlog = sum(
        int(item[2]) for item in triage_rows if str(item[0]) == "pending"
    )
    backlog_ages = tuple(
        int(item[3]) for item in triage_rows if str(item[0]) == "pending"
    )
    actionable_rows = tuple(
        item for item in triage_rows if str(item[0]) == "actionable"
    )
    finding_state = base_reporting.finding_stats(
        connection,
        repository=repository,
        expiring_at=window_ended_at,
        expiring_within_days=0,
        now=window_ended_at,
    )
    finding_totals = connection.execute(
        """
        WITH scoped_identities AS (
            SELECT identity.id
            FROM review_agent.finding_identities AS identity
            JOIN review_agent.repositories AS repository
              ON repository.id = identity.repository_id
            WHERE %s::text IS NULL
               OR lower(repository.full_name) = lower(%s::text)
        ), latest_occurrence AS (
            SELECT DISTINCT ON (occurrence.finding_id)
                   occurrence.finding_id, occurrence.context_hash
            FROM review_agent.finding_occurrences AS occurrence
            JOIN scoped_identities AS identity
              ON identity.id = occurrence.finding_id
            ORDER BY occurrence.finding_id, occurrence.observed_at DESC,
                     occurrence.id DESC
        ), latest_decision AS (
            SELECT DISTINCT ON (decision.finding_id)
                   decision.finding_id, decision.decision,
                   decision.context_hash, decision.expires_at
            FROM review_agent.finding_decisions AS decision
            JOIN scoped_identities AS identity
              ON identity.id = decision.finding_id
            ORDER BY decision.finding_id, decision.id DESC
        )
        SELECT
            count(*) FILTER (
                WHERE decision.decision = ANY(%s::text[])
                  AND decision.expires_at > %s
                  AND decision.context_hash <> occurrence.context_hash
            )::integer,
            (
                SELECT count(*)::integer
                FROM review_agent.finding_occurrences AS repeated
                JOIN scoped_identities AS identity
                  ON identity.id = repeated.finding_id
                WHERE repeated.observed_at >= %s
                  AND repeated.observed_at < %s
                  AND EXISTS (
                      SELECT 1
                      FROM review_agent.finding_decisions AS earlier
                      WHERE earlier.finding_id = repeated.finding_id
                        AND earlier.decision = ANY(%s::text[])
                        AND earlier.created_at < repeated.observed_at
                  )
            )
        FROM latest_occurrence AS occurrence
        LEFT JOIN latest_decision AS decision
          ON decision.finding_id = occurrence.finding_id
        """,
        (
            repository,
            repository,
            list(_SUPPRESSIVE_DECISIONS),
            window_ended_at,
            window_started_at,
            window_ended_at,
            list(_SUPPRESSIVE_DECISIONS),
        ),
    ).fetchone()
    if finding_totals is None:
        raise QualityReportingError("quality finding totals could not be computed")
    noisy_rows = connection.execute(
        """
        SELECT identity.rule_id, count(*)::integer
        FROM review_agent.finding_decisions AS decision
        JOIN review_agent.finding_identities AS identity
          ON identity.id = decision.finding_id
        JOIN review_agent.repositories AS repository
          ON repository.id = identity.repository_id
        WHERE decision.decision = 'false_positive'
          AND decision.created_at >= %s
          AND decision.created_at < %s
          AND (
              %s::text IS NULL
              OR lower(repository.full_name) = lower(%s::text)
          )
        GROUP BY identity.rule_id
        ORDER BY count(*) DESC, identity.rule_id
        LIMIT %s
        """,
        (
            window_started_at,
            window_ended_at,
            repository,
            repository,
            _RANKING_LIMIT,
        ),
    ).fetchall()
    coverage_rows = connection.execute(
        """
        SELECT
            CASE
                WHEN file.diff_state = 'truncated' THEN 'diff_truncated'
                ELSE file.unavailable_reason
            END AS failure_code,
            count(*)::integer
        FROM review_agent.review_run_files AS file
        JOIN review_agent.review_runs AS run ON run.id = file.review_run_id
        JOIN review_agent.pull_requests AS pull_request
          ON pull_request.id = run.pull_request_id
        JOIN review_agent.repositories AS repository
          ON repository.id = pull_request.repository_id
        WHERE run.status = 'completed'
          AND run.completed_at >= %s
          AND run.completed_at < %s
          AND file.is_changed_path
          AND file.diff_state IN ('unavailable', 'truncated')
          AND (
              %s::text IS NULL
              OR lower(repository.full_name) = lower(%s::text)
          )
        GROUP BY 1
        ORDER BY count(*) DESC, failure_code
        LIMIT %s
        """,
        (
            window_started_at,
            window_ended_at,
            repository,
            repository,
            _RANKING_LIMIT,
        ),
    ).fetchall()
    cohort_rows = connection.execute(
        """
        SELECT repository.full_name,
               COALESCE(
                   NULLIF(subject.resolved_config ->> 'profile', ''),
                   'unknown'
               ) AS profile,
               COALESCE(
                   NULLIF(
                       subject.resolved_config -> 'review_contract' ->> 'sha256',
                       ''
                   ),
                   'unknown'
               ) AS review_contract_hash,
               COALESCE(
                   NULLIF(
                       subject.resolved_config -> 'review_contract'
                           ->> 'model_provider',
                       ''
                   ),
                   'unknown'
               ) AS model_provider,
               COALESCE(
                   NULLIF(
                       subject.resolved_config -> 'review_contract' ->> 'model',
                       ''
                   ),
                   'unknown'
               ) AS model,
               subject.policy_revision,
               count(*)::integer
        FROM review_agent.review_runs AS run
        JOIN review_agent.pull_requests AS pull_request
          ON pull_request.id = run.pull_request_id
        JOIN review_agent.repositories AS repository
          ON repository.id = pull_request.repository_id
        JOIN review_agent.review_subjects AS subject
          ON subject.id = run.review_subject_id
        WHERE run.status = 'completed'
          AND run.completed_at >= %s
          AND run.completed_at < %s
          AND (
              %s::text IS NULL
              OR lower(repository.full_name) = lower(%s::text)
          )
        GROUP BY repository.full_name, profile, review_contract_hash,
                 model_provider, model, subject.policy_revision
        ORDER BY repository.full_name, profile, review_contract_hash,
                 model_provider, model, subject.policy_revision
        """,
        (
            window_started_at,
            window_ended_at,
            repository,
            repository,
        ),
    ).fetchall()
    return QualityReport(
        schema_version=1,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        window_days=window_days,
        repository=repository,
        completed_reviews=completed_reviews,
        published_findings=published_findings,
        complete_coverage_reviews=int(row[3]),
        false_positive_signals=SignalCount(
            count=int(row[4]),
            denominator=published_findings,
            denominator_name="published_findings",
        ),
        scope_confusion_signals=SignalCount(
            count=int(row[5]),
            denominator=completed_reviews,
            denominator_name="completed_reviews",
        ),
        missed_issue_signals=SignalCount(
            count=int(row[6]),
            denominator=completed_reviews,
            denominator_name="completed_reviews",
        ),
        triage_backlog=triage_backlog,
        oldest_triage_backlog_seconds=(max(backlog_ages) if backlog_ages else None),
        actionable_missed_issues_by_target_owner=tuple(
            RankedCount(value=str(item[1]), count=int(item[2]))
            for item in actionable_rows
        ),
        active_suppressions=finding_state.active_suppressions,
        suppressions_invalidated_by_context=int(finding_totals[0]),
        repeat_findings_after_suppressive_decision=int(finding_totals[1]),
        noisy_rule_ids=tuple(
            RankedCount(value=str(item[0]), count=int(item[1]))
            for item in noisy_rows
        ),
        coverage_failure_codes=tuple(
            RankedCount(value=str(item[0]), count=int(item[1]))
            for item in coverage_rows
        ),
        cohorts=tuple(
            QualityCohort(
                repository=str(item[0]),
                profile=str(item[1]),
                review_contract_hash=str(item[2]),
                model_provider=str(item[3]),
                model=str(item[4]),
                policy_revision=str(item[5]),
                completed_reviews=int(item[6]),
            )
            for item in cohort_rows
        ),
    )


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _ranked_lines(items: tuple[RankedCount, ...]) -> list[str]:
    if not items:
        return ["| — | 0 |"]
    return [f"| {_cell(item.value)} | {item.count} |" for item in items]


def render_markdown(report: QualityReport) -> str:
    """Render the report without converting absent feedback into a score."""
    scope = report.repository or "all repositories"
    lines = [
        "# Review quality",
        "",
        f"Scope: `{_cell(scope)}`  ",
        (
            "Window: `"
            + report.window_started_at.isoformat()
            + "` to `"
            + report.window_ended_at.isoformat()
            + "`"
        ),
        "",
        "## Review activity",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
        f"| Completed reviews | {report.completed_reviews} |",
        f"| Published findings | {report.published_findings} |",
        f"| Complete-coverage reviews | {report.complete_coverage_reviews} |",
        (
            "| Repeat finding occurrences after a suppressive decision | "
            f"{report.repeat_findings_after_suppressive_decision} |"
        ),
        "",
        "## Explicit signals",
        "",
        "| Signal | Count | Denominator |",
        "| --- | ---: | ---: |",
        (
            "| False-positive decisions | "
            f"{report.false_positive_signals.count} | "
            f"{report.false_positive_signals.denominator} published findings |"
        ),
        (
            "| Scope-confusion events | "
            f"{report.scope_confusion_signals.count} | "
            f"{report.scope_confusion_signals.denominator} completed reviews |"
        ),
        (
            "| Missed-issue events | "
            f"{report.missed_issue_signals.count} | "
            f"{report.missed_issue_signals.denominator} completed reviews |"
        ),
        "",
        "## Current governed state",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
        f"| Pending triage | {report.triage_backlog} |",
        (
            "| Oldest pending triage (seconds) | "
            + (
                str(report.oldest_triage_backlog_seconds)
                if report.oldest_triage_backlog_seconds is not None
                else "—"
            )
            + " |"
        ),
        f"| Active suppressions | {report.active_suppressions} |",
        (
            "| Suppressions invalidated by context | "
            f"{report.suppressions_invalidated_by_context} |"
        ),
        "",
        "## Actionable missed issues by owner",
        "",
        "| Owner | Count |",
        "| --- | ---: |",
        *_ranked_lines(report.actionable_missed_issues_by_target_owner),
        "",
        "## Noisy rule evidence",
        "",
        "| Rule ID | False-positive decisions |",
        "| --- | ---: |",
        *_ranked_lines(report.noisy_rule_ids),
        "",
        "## Coverage failure codes",
        "",
        "| Failure code | Changed paths |",
        "| --- | ---: |",
        *_ranked_lines(report.coverage_failure_codes),
        "",
        "## Completed-review cohorts",
        "",
        (
            "| Repository | Profile | Review contract | Provider | Model | "
            "Policy revision | Reviews |"
        ),
        "| --- | --- | --- | --- | --- | --- | ---: |",
    ]
    if report.cohorts:
        lines.extend(
            (
                f"| {_cell(item.repository)} | {_cell(item.profile)} | "
                f"{_cell(item.review_contract_hash)} | "
                f"{_cell(item.model_provider)} | {_cell(item.model)} | "
                f"{_cell(item.policy_revision)} | {item.completed_reviews} |"
            )
            for item in report.cohorts
        )
    else:
        lines.append("| — | — | — | — | — | — | 0 |")
    return "\n".join(lines) + "\n"
