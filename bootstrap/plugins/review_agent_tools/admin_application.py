"""Read-only application boundary for the operator web panel."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import TupleRow

from . import operator_application
from .deployment_settings import DeploymentSettings
from .postgres import deployment_settings as settings_store
from .domain.feedback import resolve_github_repository, resolve_repository
from .github import app_auth, app_inventory
from .postgres import github_app, repository_requests
from .domain.review import ReviewRunId
from .postgres import (
    admin_operations,
    admin_reporting,
    admin_quality,
    admin_run_actions,
    quality_reporting,
    quality_triage,
    audit,
    team_access,
    teams,
)
from .postgres.team_access import AccessRequest, AccessScope, TeamRole
from .postgres.runtime import PostgreSQLRuntime


@contextmanager
def _transaction(
    runtime: PostgreSQLRuntime,
    access: AccessRequest,
    *,
    write: bool = False,
    access_change: bool = False,
) -> Iterator[tuple[psycopg.Connection[TupleRow], AccessScope]]:
    with runtime.transaction() as connection:
        if access_change:
            team_access.lock_access_change(connection)
        elif not write:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        scope = team_access.resolve_scope(
            connection, access, write=write and not access_change
        )
        yield connection, scope


def list_teams(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    limit: int,
    after_id: int,
    search: str,
) -> teams.TeamPage:
    with _transaction(runtime, access) as (connection, scope):
        return teams.list_teams(
            connection, scope, limit=limit, after_id=after_id, search=search
        )


def get_team(
    runtime: PostgreSQLRuntime, *, access: AccessRequest, team_id: int
) -> teams.Team:
    with _transaction(runtime, access) as (connection, scope):
        return teams.get_team(connection, scope, team_id)


def create_team(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    name: str,
    description: str,
    reason: str,
) -> teams.Team:
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        return teams.create_team(
            connection, scope, name=name, description=description, reason=reason
        )


def update_team(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    name: str,
    description: str,
    expected_revision: int,
    reason: str,
) -> teams.Team:
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        return teams.update_team(
            connection,
            scope,
            team_id=team_id,
            name=name,
            description=description,
            expected_revision=expected_revision,
            reason=reason,
        )


def team_members(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    limit: int,
    offset: int,
) -> teams.TeamMemberPage:
    with _transaction(runtime, access) as (connection, scope):
        return teams.members(
            connection, scope, team_id=team_id, limit=limit, offset=offset
        )


def put_team_member(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    email: str,
    role: TeamRole,
    reason: str,
) -> teams.TeamMember:
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        return teams.put_member(
            connection, scope, team_id=team_id, email=email, role=role, reason=reason
        )


def remove_team_member(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    user_id: UUID,
    reason: str,
) -> None:
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        teams.remove_member(
            connection, scope, team_id=team_id, user_id=user_id, reason=reason
        )


def team_events(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    limit: int,
    before_id: int | None,
) -> audit.AuditPage:
    with _transaction(runtime, access) as (connection, scope):
        return audit.events(
            connection,
            scope,
            team_id=team_id,
            limit=limit,
            filters=audit.AuditFilters(before_id=before_id),
        )


def repositories(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
    search: str = "",
    offset: int = 0,
    limit: int = 50,
) -> admin_reporting.RepositoryPage:
    _bounds(days=days, limit=limit)
    if not 0 <= offset <= 10000 or len(search) > 200:
        raise ValueError("repository filter exceeds its bounds")
    since, until, now = report_window(days=days, start=start, end=end)
    with _transaction(runtime, access) as (connection, scope):
        return admin_reporting.repositories(
            connection,
            scope=scope,
            since=since,
            until=until,
            now=now,
            days=days,
            search=search.strip(),
            offset=offset,
            limit=limit,
        )


def history(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
    repository: str | None = None,
    status: admin_reporting.HistoryStatus = "all",
    pr_number: int | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> admin_reporting.HistoryPage:
    normalized = _history_scope(
        days=days,
        limit=limit,
        repository=repository,
        status=status,
        pr_number=pr_number,
        before_id=before_id,
    )
    since, until, now = report_window(days=days, start=start, end=end)
    with _transaction(runtime, access) as (connection, scope):
        return admin_reporting.history(
            connection,
            scope=scope,
            since=since,
            until=until,
            now=now,
            days=days,
            repository=normalized,
            status=status,
            pr_number=pr_number,
            limit=limit,
            before_id=before_id,
        )


def _bounds(*, days: int, limit: int) -> None:
    if not 1 <= days <= 90 or not 1 <= limit <= 100:
        raise ValueError("days must be 1–90 and limit must be 1–100")


def review_detail(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    run_id: int,
    before_id: int | None = None,
) -> admin_reporting.ReviewDetail | None:
    if run_id < 1 or (before_id is not None and before_id < 1):
        raise ValueError("Request ID and cursor must be positive")
    with _transaction(runtime, access) as (connection, scope):
        result = admin_reporting.review_detail(
            connection,
            scope=scope,
            run_id=ReviewRunId(run_id),
            before_id=before_id,
            now=datetime.now(timezone.utc),
        )
        return (
            replace(
                result,
                can_maintain=team_access.can_maintain(
                    connection, scope, result.item.repository
                ),
            )
            if result is not None
            else None
        )


def report_window(
    *, days: int, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime, datetime]:
    now = datetime.now(timezone.utc)
    if (start is None) != (end is None):
        raise ValueError("Provide both start and end")
    if start is None or end is None:
        return now - timedelta(days=days), now, now
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Use timestamps with a timezone offset")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if not start < end or end - start > timedelta(days=366):
        raise ValueError("The time range must be positive and at most 366 days")
    return start, end, now


def overview(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
) -> admin_operations.Overview:
    _bounds(days=days, limit=1)
    window_start, window_end, now = report_window(days=days, start=start, end=end)
    with _transaction(runtime, access) as (connection, scope):
        return admin_operations.overview(
            connection, scope=scope, start=window_start, end=window_end, now=now
        )


def operations(
    runtime: PostgreSQLRuntime, *, access: AccessRequest
) -> admin_operations.Operations:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_admin(scope)
        return admin_operations.operations(connection, now=datetime.now(timezone.utc))


def events(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    limit: int = 50,
    worker_id: UUID | None = None,
    before_id: int | None = None,
) -> admin_operations.WorkerEventPage:
    _bounds(days=1, limit=limit)
    if before_id is not None and before_id < 1:
        raise ValueError("The cursor must be positive")
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_admin(scope)
        return admin_operations.events(
            connection, worker_id=worker_id, before_id=before_id, limit=limit
        )


def pull_requests(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
    repository: str | None = None,
    status: admin_reporting.HistoryStatus = "all",
    pr_number: int | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> admin_reporting.PullRequestPage:
    normalized = _history_scope(
        days=days,
        limit=limit,
        repository=repository,
        status=status,
        pr_number=pr_number,
        before_id=before_id,
    )
    since, until, now = report_window(days=days, start=start, end=end)
    with _transaction(runtime, access) as (connection, scope):
        return admin_reporting.pull_requests(
            connection,
            scope=scope,
            since=since,
            until=until,
            now=now,
            repository=normalized,
            status=status,
            pr_number=pr_number,
            limit=limit,
            before_id=before_id,
        )


def _history_scope(
    *,
    days: int,
    limit: int,
    repository: str | None,
    status: admin_reporting.HistoryStatus,
    pr_number: int | None,
    before_id: int | None,
) -> str | None:
    _bounds(days=days, limit=limit)
    if status not in (
        "all",
        "active",
        "published",
        "failed",
        "latest_failed",
        "superseded",
    ):
        raise ValueError("unsupported history status")
    if any(value is not None and value < 1 for value in (pr_number, before_id)):
        raise ValueError("PR number and cursor must be positive")
    return resolve_repository(repository) if repository is not None else None


def quality_report(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    repository: str | None,
    days: int,
) -> quality_reporting.QualityReport:
    _bounds(days=days, limit=1)
    moment = datetime.now(timezone.utc)
    normalized = resolve_repository(repository) if repository is not None else None
    with _transaction(runtime, access) as (connection, scope):
        if normalized is not None:
            team_access.require_repository(connection, scope, normalized)
        return quality_reporting.build_report(
            connection,
            scope=scope,
            repository=normalized,
            window_started_at=moment - timedelta(days=days),
            window_ended_at=moment,
            window_days=days,
        )


def quality_feedback(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    repository: str | None,
    limit: int,
    offset: int,
) -> admin_quality.QualityFeedbackPage:
    with _transaction(runtime, access) as (connection, scope):
        return admin_quality.feedback_backlog(
            connection, scope=scope, repository=repository, limit=limit, offset=offset
        )


def show_finding(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    repository: str,
    fingerprint: str,
    occurrence_id: int | None,
    decisions_before_id: int | None,
) -> admin_quality.AdminFindingDetail:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_repository(connection, scope, repository)
        detail = operator_application.show_finding_in_transaction(
            connection,
            repository=repository,
            fingerprint=fingerprint,
            occurrence_id=occurrence_id,
            decision_limit=101,
            decision_before_id=decisions_before_id,
        )
        decisions = detail.decisions[:100]
        return admin_quality.AdminFindingDetail(
            finding=detail.finding,
            decisions=decisions,
            can_decide=team_access.can_maintain(connection, scope, repository),
            has_more_decisions=len(detail.decisions) > 100,
            next_decision_before_id=int(decisions[-1].id)
            if len(detail.decisions) > 100
            else None,
        )


def review_findings(
    runtime: PostgreSQLRuntime, *, access: AccessRequest, run_id: int
) -> admin_quality.ReviewFindingPage:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_run(connection, scope, run_id)
        return admin_quality.review_findings(connection, run_id=run_id)


def decide_finding(
    runtime: PostgreSQLRuntime,
    request: operator_application.OperatorDecisionRequest,
    *,
    access: AccessRequest,
) -> operator_application.OperatorDecisionResult:
    with _transaction(runtime, access, write=True) as (connection, scope):
        repository_id = team_access.require_repository(
            connection, scope, request.repository, maintain=True
        )
        result = operator_application.decide_finding_in_transaction(
            connection, replace(request, actor=scope.actor)
        )
        audit.record(
            connection,
            scope,
            action=audit.AuditAction.FINDING_DECIDED,
            subject=str(result.id),
            reason=request.reason,
            repository_id=repository_id,
            details={
                "occurrence_id": int(result.occurrence_id),
                "decision": result.decision.value,
            },
        )
        return result


def triage_review_feedback(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    feedback_id: int,
    status: str,
    reason: str,
    stable_key: str,
    target_owner: str,
    evidence_reference: str,
    path: str,
    category: str,
) -> quality_triage.QualityFeedbackTriage:
    with _transaction(runtime, access, write=True) as (connection, scope):
        repository_id = team_access.require_feedback(
            connection, scope, feedback_id, maintain=True
        )
        result = operator_application.triage_review_feedback_in_transaction(
            connection,
            feedback_id=feedback_id,
            status=status,
            reason=reason,
            stable_key=stable_key,
            target_owner=target_owner,
            evidence_reference=evidence_reference,
            path=path,
            category=category,
            actor=scope.actor,
        )
        audit.record(
            connection,
            scope,
            action=audit.AuditAction.FEEDBACK_TRIAGED,
            subject=str(feedback_id),
            reason=reason,
            repository_id=repository_id,
            details={"status": status},
        )
        return result


def _run_controls(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    run_id: int,
    *,
    stale_after_minutes: int = 15,
) -> admin_run_actions.RunControls:
    result = admin_run_actions.controls(
        connection, run_id=ReviewRunId(run_id), stale_after_minutes=stale_after_minutes
    )
    return (
        result
        if scope.is_admin
        else replace(
            result,
            mark_stalled=admin_run_actions.ActionAvailability(
                False, "Platform administrator access required"
            ),
        )
    )


def run_controls(
    runtime: PostgreSQLRuntime, *, access: AccessRequest, run_id: int
) -> admin_run_actions.RunControls:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_run(connection, scope, run_id, maintain=True)
        return _run_controls(connection, scope, run_id)


def apply_run_action(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    run_id: int,
    request: admin_run_actions.ActionRequest,
) -> admin_run_actions.RunControls:
    with _transaction(runtime, access, write=True) as (connection, scope):
        repository_id = team_access.require_run(
            connection, scope, run_id, maintain=True
        )
        if request.action is admin_run_actions.RunAction.MARK_STALLED:
            team_access.require_admin(scope)
        admin_run_actions.apply_action(
            connection,
            run_id=ReviewRunId(run_id),
            request=replace(request, actor=scope.actor),
        )
        audit.record(
            connection,
            scope,
            action=audit.AuditAction.RUN_ACTION,
            subject=str(run_id),
            reason=request.reason,
            repository_id=repository_id,
            details={"action": request.action.value},
            owner_only=request.action is admin_run_actions.RunAction.MARK_STALLED,
        )
        return _run_controls(
            connection,
            scope,
            run_id,
            stale_after_minutes=request.stale_after_minutes or 15,
        )


def audit_events(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    limit: int,
    filters: audit.AuditFilters,
) -> audit.AuditPage:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_admin(scope)
        return audit.events(
            connection,
            scope,
            team_id=scope.team_id,
            limit=limit,
            filters=filters,
        )


@dataclass(frozen=True, slots=True)
class DeploymentSettingsPage:
    settings: DeploymentSettings
    revision: int
    history: tuple[settings_store.SettingsRevision, ...]
    next_before_id: int | None
    startup_loads: tuple[settings_store.ServiceSettingsLoad, ...]


def deployment_settings(
    runtime: PostgreSQLRuntime, *, access: AccessRequest, before_id: int | None
) -> DeploymentSettingsPage:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_owner(scope)
        current = settings_store.latest(connection)
        revisions = settings_store.history(connection, before_id=before_id)
        loads = settings_store.startup_loads(connection)
        return DeploymentSettingsPage(
            current.settings if current else DeploymentSettings.from_environment(),
            current.id if current else 0,
            revisions[:20],
            revisions[19].id if len(revisions) > 20 else None,
            loads,
        )


def save_deployment_settings(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    settings: DeploymentSettings,
    expected_revision: int,
    reason: str,
) -> settings_store.SettingsRevision:
    with _transaction(runtime, access, write=True) as (connection, scope):
        team_access.require_owner(scope)
        saved = settings_store.save(
            connection,
            settings=settings,
            expected_revision=expected_revision,
            actor=scope.actor,
            reason=reason,
        )
        audit.record(
            connection,
            scope,
            action=audit.AuditAction.SETTINGS_UPDATED,
            subject=str(saved.id),
            reason=reason,
            details={"previous_revision": expected_revision, "revision": saved.id},
            owner_only=True,
        )
        return saved


def repository_request_list(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    status: repository_requests.RequestStatus | None,
    limit: int,
    before_id: int | None,
) -> repository_requests.RepositoryRequestPage:
    with _transaction(runtime, access) as (connection, scope):
        return repository_requests.list_requests(
            connection, scope, status=status, limit=limit, before_id=before_id
        )


def submit_repository_request(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    repository: str,
    reason: str,
) -> repository_requests.RepositoryRequest:
    normalized = resolve_github_repository(repository)
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        return repository_requests.submit(
            connection, scope, team_id=team_id, repository=normalized, reason=reason
        )


def finish_repository_request(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    request_id: int,
    status: repository_requests.RequestStatus,
    reason: str,
) -> repository_requests.RepositoryRequest:
    if status not in (
        repository_requests.RequestStatus.REJECTED,
        repository_requests.RequestStatus.WITHDRAWN,
    ):
        raise ValueError("Use repository approval to enable reviews")
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        return repository_requests.finish(
            connection, scope, request_id=request_id, status=status, reason=reason
        )


def approve_repository_request(
    runtime: PostgreSQLRuntime,
    authenticator: app_auth.GitHubAppAuthenticator,
    *,
    access: AccessRequest,
    request_id: int,
    profile: str,
    reason: str,
) -> repository_requests.RepositoryRequest:
    with _transaction(runtime, access) as (connection, scope):
        team_access.require_admin(scope)
        request = repository_requests.get_request(connection, scope, request_id)
        if request.status is repository_requests.RequestStatus.APPROVED:
            return request
        if request.status is not repository_requests.RequestStatus.PENDING:
            raise teams.TeamConflict("This request already has a different decision")
        observation = github_app.observe_repository_access(
            connection, repository=request.repository_name
        )
    inventory = app_inventory.read_repository_inventory(
        authenticator, repository=request.repository_name
    )
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        team_access.require_admin(scope)
        current = repository_requests.get_request(
            connection, scope, request_id, for_update=True
        )
        if current.status is repository_requests.RequestStatus.APPROVED:
            return current
        if current.status is not repository_requests.RequestStatus.PENDING:
            raise teams.TeamConflict("This request already has a different decision")
        if (
            inventory.repository.full_name.casefold()
            != current.repository_name.casefold()
        ):
            raise teams.TeamConflict(
                "The repository name changed. Withdraw this request and submit its current name"
            )
        _, enabled = github_app.accept_verified_repository(
            connection,
            definition=inventory.installation.definition,
            status=inventory.installation.status,
            repository=inventory.repository,
            observation=observation,
            profile=profile,
            actor=scope.actor,
            reason=reason,
        )
        repository_requests.assign(
            connection,
            scope,
            repository_id=int(enabled.repository_id),
            team_id=current.team_id,
            expected_team_id=None,
            reason=reason,
        )
        return repository_requests.finish(
            connection,
            scope,
            request_id=request_id,
            status=repository_requests.RequestStatus.APPROVED,
            repository_id=int(enabled.repository_id),
            reason=reason,
        )


def team_repositories(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    team_id: int,
    limit: int,
    after_id: int,
) -> repository_requests.TeamRepositoryPage:
    with _transaction(runtime, access) as (connection, scope):
        return repository_requests.repositories(
            connection, scope, team_id=team_id, limit=limit, after_id=after_id
        )


def assign_team_repository(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    repository_id: int,
    team_id: int,
    expected_team_id: int | None,
    reason: str,
) -> None:
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        repository_requests.assign(
            connection,
            scope,
            repository_id=repository_id,
            team_id=team_id,
            expected_team_id=expected_team_id,
            reason=reason,
        )


def remove_team_repository(
    runtime: PostgreSQLRuntime,
    *,
    access: AccessRequest,
    repository_id: int,
    team_id: int,
    reason: str,
) -> None:
    with _transaction(runtime, access, access_change=True) as (connection, scope):
        repository_requests.remove(
            connection,
            scope,
            repository_id=repository_id,
            team_id=team_id,
            reason=reason,
        )
