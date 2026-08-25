"""Application orchestration for the existing publication lifecycle."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain.review import ReviewRunId
    from .postgres.runtime import PostgreSQLRuntime

from . import failure_codes, review_run_application
from .domain.publication import PublicationPartType
from .github.publication import (
    GitHubPublicationError,
    GitHubPublicationGateway,
    InlineReviewComment,
    IssueComment,
    PullRequestReviewComment,
    PullRequestState,
)
from .publication_partition import (
    HistoricalPublication,
    extra_superseded_body,
    historical_bodies,
)
from .review_identity import REVIEW_COMMENT_TITLE

_COMMENT_RECOVERY_SCAN_PAGES = 10


@dataclass(frozen=True, slots=True)
class PostgresPublishedPart:
    part_type: PublicationPartType
    external_id: int


@dataclass(frozen=True, slots=True)
class PostgresPublicationResult:
    publication_id: int
    status: str
    published_parts: tuple[PostgresPublishedPart, ...]
    recovered_parts: int
    superseded_publication_id: int | None = None
    supersession_rendered: bool | None = None
    supersession_failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class PostgresSupersessionResult:
    publication_id: int
    rendered: bool
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class PostgresFailureStatusResult:
    run_id: int
    comment_id: int
    failure_code: str
    posted_at: str


@dataclass(frozen=True, slots=True)
class PreparedPostgresPublication:
    publication_id: int
    findings_count: int
    suggestions_count: int
    resolved_count: int
    ignored_previous_verdicts: tuple[str, ...]


def prepare_postgres_publication(
    runtime: "PostgreSQLRuntime",
    *,
    run_id: int,
    previous_verdicts: object,
    feedback_enabled: bool,
    max_comment_bytes: int,
    delivery_max_attempts: int = 3,
    review_job_id: int | None = None,
    review_lease_generation: int | None = None,
) -> PreparedPostgresPublication:
    """Build and freeze one exact plan in a single bounded transaction."""
    from .domain.publication import (
        PublicationFindingOutcome,
        PublicationId,
        SuggestionReviewDelivery,
    )
    from .domain.review import ReviewRunId
    from .postgres import publications as postgres_publications
    from .review_publication_planner import build_publication

    resolved_run_id = ReviewRunId(run_id)
    with runtime.transaction() as connection:
        existing = connection.execute(
            "SELECT id FROM review_agent.publications WHERE review_run_id = %s",
            (resolved_run_id,),
        ).fetchone()
        if existing is not None:
            stored = postgres_publications.get_publication(
                connection, PublicationId(int(existing[0]))
            )
            return PreparedPostgresPublication(
                publication_id=int(stored.id),
                findings_count=sum(
                    item.outcome is PublicationFindingOutcome.CURRENT
                    for item in stored.plan.findings
                ),
                suggestions_count=sum(
                    len(part.delivery.comments)
                    for part in stored.parts
                    if isinstance(part.delivery, SuggestionReviewDelivery)
                ),
                resolved_count=sum(
                    item.outcome is PublicationFindingOutcome.RESOLVED
                    for item in stored.plan.findings
                ),
                ignored_previous_verdicts=(),
            )
        context = postgres_publications.preparation_context(
            connection, run_id=resolved_run_id
        )
        planned = build_publication(
            context,
            previous_verdicts=previous_verdicts,
            feedback_enabled=feedback_enabled,
            max_comment_bytes=max_comment_bytes,
        )
        stored = postgres_publications.prepare_publication(
            connection,
            run_id=resolved_run_id,
            plan=planned.plan,
            delivery_max_attempts=delivery_max_attempts,
            review_job_id=review_job_id,
            review_lease_generation=review_lease_generation,
        )
    return PreparedPostgresPublication(
        publication_id=int(stored.id),
        findings_count=planned.findings_count,
        suggestions_count=planned.suggestions_count,
        resolved_count=planned.resolved_count,
        ignored_previous_verdicts=planned.ignored_previous_verdicts,
    )


def _postgres_target_failure(
    *, base_sha: str, head_sha: str, pull: PullRequestState
) -> str | None:
    if pull.state != "open":
        return "pr_not_open"
    if pull.base_sha != base_sha:
        return "base_sha_changed"
    if pull.head_sha != head_sha:
        return "head_sha_changed"
    return None


def _render_postgres_supersession(
    runtime: "PostgreSQLRuntime",
    *,
    github: GitHubPublicationGateway,
    superseding_publication_id: int,
    max_comment_bytes: int,
) -> PostgresSupersessionResult | None:
    """Rewrite one prior PostgreSQL publication without holding a checkout."""
    from .domain.publication import PublicationDomainError, PublicationId
    from .postgres import publications as postgres_publications

    with runtime.transaction() as connection:
        historical = postgres_publications.publication_for_supersession(
            connection,
            superseding_publication_id=PublicationId(superseding_publication_id),
        )
    if historical is None:
        return None

    partition_input: HistoricalPublication = {
        "review_number": historical.review_number,
        "repository": historical.repository,
        "pr_number": historical.pr_number,
        "head_sha": historical.head_sha,
        "publication_key": historical.publication_key,
        "rendered_markdown": historical.rendered_markdown,
        "rendered_blocks_json": historical.rendered_blocks_json,
        "current_findings_count": historical.current_findings_count,
        "superseded_by_review_number": historical.superseding_review_number,
        "superseded_by_comment_id": historical.superseding_comment_id,
    }

    try:
        comments = github.list_issue_comments(
            historical.repository,
            historical.pr_number,
            max_pages=_COMMENT_RECOVERY_SCAN_PAGES,
            newest_first=True,
        )
        targets = _comments_by_id(comments, list(historical.comment_ids))
        if len(targets) != len(historical.comment_ids):
            raise PublicationDomainError("superseded_comment_missing")
        parts = historical_bodies(
            partition_input,
            max_comment_bytes=max_comment_bytes,
            target_parts=len(targets),
        )
        if len(parts) > len(targets):
            raise GitHubPublicationError("superseded_body_needs_more_parts")
        for index, target in enumerate(targets):
            body = (
                parts[index].body
                if index < len(parts)
                else extra_superseded_body(
                    partition_input,
                    part_number=index + 1,
                    total_parts=len(targets),
                )
            )
            if target.body != body:
                github.update_issue_comment(
                    historical.repository,
                    target.comment_id,
                    body,
                )
    except (GitHubPublicationError, PublicationDomainError, ValueError) as exc:
        code = (
            exc.code
            if isinstance(exc, (GitHubPublicationError, PublicationDomainError))
            else "supersession_failed"
        )
        with runtime.transaction() as connection:
            postgres_publications.record_supersession_result(
                connection,
                publication_id=historical.publication_id,
                failure_code=code,
            )
        return PostgresSupersessionResult(
            publication_id=int(historical.publication_id),
            rendered=False,
            failure_code=code,
        )

    with runtime.transaction() as connection:
        postgres_publications.record_supersession_result(
            connection,
            publication_id=historical.publication_id,
            failure_code=None,
        )
    return PostgresSupersessionResult(
        publication_id=int(historical.publication_id),
        rendered=True,
    )


def publish_postgres_run_failure_status(
    runtime: "PostgreSQLRuntime",
    *,
    run_id: int,
    github: GitHubPublicationGateway,
    lease_owner: str | None = None,
    lease_generation: int | None = None,
    retry_delay: timedelta = timedelta(minutes=1),
    lease_lost: threading.Event | None = None,
) -> PostgresFailureStatusResult:
    """Deliver one leased deterministic terminal status with exact recovery."""
    from .domain.review import FailureStatusDelivery, ReviewRunId
    from .postgres import review_runs as postgres_review_runs

    resolved_id = ReviewRunId(run_id)
    owner = lease_owner
    generation = lease_generation
    if owner is None or generation is None:
        with runtime.transaction() as connection:
            existing_target = postgres_review_runs.failure_status_target(
                connection, resolved_id
            )
        if (
            existing_target.delivery_status is FailureStatusDelivery.POSTED
            and existing_target.comment_id is not None
            and existing_target.posted_at is not None
        ):
            return PostgresFailureStatusResult(
                run_id=run_id, comment_id=existing_target.comment_id,
                failure_code=existing_target.failure_code,
                posted_at=existing_target.posted_at.isoformat().replace("+00:00", "Z"),
            )
        owner = f"operator-recovery-{run_id}"
        with runtime.transaction() as connection:
            claim = postgres_review_runs.claim_failure_status(
                connection, run_id=resolved_id, lease_owner=owner,
                lease_duration=timedelta(minutes=5),
            )
        generation = claim.target.delivery_lease_generation
    with runtime.transaction() as connection:
        target = postgres_review_runs.require_live_failure_status_lease(
            connection, run_id=resolved_id, lease_owner=owner,
            lease_generation=generation,
        )
    body = _failure_status_body(run_id, target.head_sha, target.failure_code)
    try:
        listed_markers = _my_failure_status_comments(
            github, target.repository, target.pr_number
        )
        current_markers = _cleanup_postgres_failure_status(
            runtime, github=github, repository=target.repository,
            pr_number=target.pr_number, exclude_run_id=resolved_id,
            known_markers=listed_markers,
        )
        marker = _failure_status_marker(run_id, target.head_sha)
        existing = next(
            (item for item in current_markers if marker in item.body), None
        )
        if lease_lost is not None and lease_lost.is_set():
            raise postgres_review_runs.FailureStatusLeaseLost("failure-status lease was lost")
        with runtime.transaction() as connection:
            postgres_review_runs.require_live_failure_status_lease(
                connection, run_id=resolved_id, lease_owner=owner,
                lease_generation=generation,
            )
        if lease_lost is not None and lease_lost.is_set():
            raise postgres_review_runs.FailureStatusLeaseLost("failure-status lease was lost")
        comment = (
            github.update_issue_comment(target.repository, existing.comment_id, body)
            if existing is not None
            else github.create_issue_comment(target.repository, target.pr_number, body)
        )
    except GitHubPublicationError as exc:
        with runtime.transaction() as connection:
            postgres_review_runs.retry_failure_status(
                connection, run_id=resolved_id, lease_owner=owner,
                lease_generation=generation,
                failure_code=f"github_{exc.status or 'error'}", retry_delay=retry_delay,
            )
        raise
    with runtime.transaction() as connection:
        recorded = postgres_review_runs.complete_failure_status(
            connection, run_id=resolved_id, lease_owner=owner,
            lease_generation=generation, comment_id=comment.comment_id,
        )
    if recorded.posted_at is None:
        raise postgres_review_runs.ReviewRunError(
            "failure-status comment has no recorded timestamp"
        )
    _cleanup_postgres_failure_status(
        runtime, github=github, repository=recorded.repository,
        pr_number=recorded.pr_number, exclude_run_id=resolved_id,
    )
    return PostgresFailureStatusResult(
        run_id=run_id,
        comment_id=comment.comment_id,
        failure_code=recorded.failure_code,
        posted_at=recorded.posted_at.isoformat().replace("+00:00", "Z"),
    )


def _cleanup_postgres_failure_status(
    runtime: "PostgreSQLRuntime",
    *,
    github: GitHubPublicationGateway,
    repository: str,
    pr_number: int,
    scan_markers: bool = True,
    exclude_run_id: "ReviewRunId | None" = None,
    known_markers: Sequence[IssueComment] | None = None,
) -> tuple[IssueComment, ...]:
    """Remove stored and marker-recovered failure statuses after a real review."""
    from .postgres import review_runs as postgres_review_runs

    with runtime.transaction() as connection:
        postgres_review_runs.suppress_unposted_failure_statuses_for_pull_request(
            connection,
            repository=repository,
            pr_number=pr_number,
            exclude_run_id=exclude_run_id,
        )
        targets = postgres_review_runs.failure_status_comments_for_pull_request(
            connection,
            repository=repository,
            pr_number=pr_number,
            exclude_run_id=exclude_run_id,
        )
    deleted: set[int] = set()
    retained: set[int] = set()
    for target in targets:
        try:
            github.delete_issue_comment(repository, target.comment_id)
        except GitHubPublicationError as exc:
            if exc.status != 404:
                retained.add(target.comment_id)
                continue
        deleted.add(target.comment_id)
        with runtime.transaction() as connection:
            postgres_review_runs.clear_failure_status_comment(
                connection,
                run_id=target.run_id,
            )
    if not scan_markers:
        return ()
    if known_markers is None:
        try:
            marker_comments = _my_failure_status_comments(github, repository, pr_number)
        except GitHubPublicationError:
            return ()
    else:
        marker_comments = known_markers
    current_markers: list[IssueComment] = []
    for comment in marker_comments:
        if exclude_run_id is not None and f"run={int(exclude_run_id)} " in comment.body:
            current_markers.append(comment)
            continue
        if comment.comment_id in deleted or comment.comment_id in retained:
            continue
        try:
            github.delete_issue_comment(repository, comment.comment_id)
        except GitHubPublicationError:
            pass
    return tuple(current_markers)


def publish_postgres_publication(
    runtime: "PostgreSQLRuntime",
    *,
    publication_id: int,
    github: GitHubPublicationGateway,
    max_comment_bytes: int,
    recover_posting: bool = False,
    lease_owner: str | None = None,
    lease_generation: int | None = None,
    retry_delay: timedelta = timedelta(seconds=30),
    lease_lost: threading.Event | None = None,
) -> PostgresPublicationResult:
    """Deliver one prepared PostgreSQL plan without holding a database checkout.

    ``recover_posting`` requires the caller to establish that the prior poster
    is no longer running.
    """
    from .domain.publication import (
        PublicationFindingOutcome,
        PublicationId,
        PublicationPartStatus,
        PublicationPartType,
        PublicationStatus,
        SuggestionReviewDelivery,
        extract_publication_key,
        publication_marker,
    )
    from .postgres import publications as postgres_publications
    from .postgres import review_runs as postgres_review_runs

    resolved_id = PublicationId(publication_id)
    with runtime.transaction() as connection:
        if lease_owner is None and lease_generation is None:
            claim = postgres_publications.claim_publication(
                connection,
                resolved_id,
                recover_expired=recover_posting,
            )
        elif lease_owner is not None and lease_generation is not None:
            publication = postgres_publications.get_publication(
                connection, resolved_id
            )
            postgres_publications.require_live_publication_lease(
                connection,
                publication_id=resolved_id,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
            claim = postgres_publications.PublicationClaim(
                publication=publication, acquired=True
            )
        else:
            raise postgres_publications.PublicationStoreError(
                "lease_owner and lease_generation must be supplied together"
            )
    publication = claim.publication
    if publication.status is PublicationStatus.POSTED:
        supersession = _render_postgres_supersession(
            runtime,
            github=github,
            superseding_publication_id=publication_id,
            max_comment_bytes=max_comment_bytes,
        )
        _cleanup_postgres_failure_status(
            runtime,
            github=github,
            repository=publication.repository,
            pr_number=publication.pr_number,
            scan_markers=False,
        )
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=PublicationStatus.POSTED.value,
            published_parts=tuple(
                PostgresPublishedPart(part.part_type, part.external_id)
                for part in publication.parts
                if part.external_id is not None
            ),
            recovered_parts=0,
            superseded_publication_id=(
                supersession.publication_id if supersession is not None else None
            ),
            supersession_rendered=(
                supersession.rendered if supersession is not None else None
            ),
            supersession_failure_code=(
                supersession.failure_code if supersession is not None else None
            ),
        )
    if not claim.acquired:
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=PublicationStatus.POSTING.value,
            published_parts=tuple(
                PostgresPublishedPart(part.part_type, part.external_id)
                for part in publication.parts
                if part.external_id is not None
            ),
            recovered_parts=0,
        )
    resolved_lease_owner = publication.delivery_lease_owner
    if resolved_lease_owner is None:
        raise postgres_publications.InvalidPublicationTransition(
            "posting publication has no delivery lease owner"
        )
    resolved_lease_generation = publication.delivery_lease_generation
    posting_started_at = publication.posting_started_at
    if posting_started_at is None:
        raise postgres_publications.InvalidPublicationTransition(
            "posting publication has no generation timestamp"
        )

    recovered = 0
    findings_count = sum(
        finding.outcome is PublicationFindingOutcome.CURRENT
        for finding in publication.plan.findings
    )
    author_login = ""
    issue_comments: dict[int, list[IssueComment]] | None = None
    review_comments: list[PullRequestReviewComment] | None = None

    def acknowledge(
        part_type: PublicationPartType, part_number: int, external_id: int
    ) -> None:
        with runtime.transaction() as connection:
            postgres_publications.acknowledge_part(
                connection,
                publication_id=resolved_id,
                part_type=part_type,
                part_number=part_number,
                external_id=external_id,
                posting_started_at=posting_started_at,
                lease_owner=resolved_lease_owner,
                lease_generation=resolved_lease_generation,
            )

    def prove_provider_write() -> None:
        if lease_lost is not None and lease_lost.is_set():
            raise postgres_publications.PublicationLeaseLost(
                "publication delivery lease is no longer current"
            )
        with runtime.transaction() as connection:
            postgres_publications.require_live_publication_lease(
                connection,
                publication_id=resolved_id,
                lease_owner=resolved_lease_owner,
                lease_generation=resolved_lease_generation,
            )
        if lease_lost is not None and lease_lost.is_set():
            raise postgres_publications.PublicationLeaseLost(
                "publication delivery lease is no longer current"
            )

    def stale_failure() -> str | None:
        pull = github.get_pull_request(publication.repository, publication.pr_number)
        return _postgres_target_failure(
            base_sha=publication.base_sha,
            head_sha=publication.head_sha,
            pull=pull,
        )

    def recovered_issue_comments(
        comments: Sequence[IssueComment],
    ) -> dict[int, list[IssueComment]]:
        found: dict[int, list[IssueComment]] = {}
        for comment in comments:
            if (
                comment.author_login.casefold() != author_login.casefold()
                or extract_publication_key(comment.body)
                != publication.plan.publication_key
            ):
                continue
            token = f"{publication_marker(publication.plan.publication_key)} part="
            token_index = comment.body.find(token)
            if token_index < 0:
                continue
            raw_number = comment.body[token_index + len(token) :].split("/", 1)[0]
            if not raw_number.isdigit() or int(raw_number) < 1:
                continue
            found.setdefault(int(raw_number), []).append(comment)
        return found

    def terminalize_stale(failure_code: str) -> PostgresPublicationResult:
        with runtime.transaction() as connection:
            postgres_review_runs.lock_run(connection, publication.review_run_id)
            postgres_publications.fail_publication(
                connection,
                publication_id=resolved_id,
                posting_started_at=posting_started_at,
                failure_code=failure_code,
                stale=True,
                retryable=False,
                lease_owner=resolved_lease_owner,
                lease_generation=resolved_lease_generation,
            )
            review_run_application.fail_run_after_publication_in_transaction(
                connection,
                publication.review_run_id,
                failure_code=failure_code,
                findings_count=findings_count,
            )
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=PublicationStatus.STALE.value,
            published_parts=(),
            recovered_parts=recovered,
        )

    try:
        for part in publication.parts:
            if part.status is PublicationPartStatus.POSTED:
                continue
            if not author_login:
                author_login = github.current_user_login()
            external_id: int | None = None
            if part.part_type in {
                PublicationPartType.SUMMARY,
                PublicationPartType.CONTINUATION,
            }:
                if issue_comments is None:
                    listed = github.list_issue_comments(
                        publication.repository,
                        publication.pr_number,
                        max_pages=_COMMENT_RECOVERY_SCAN_PAGES,
                        newest_first=True,
                    )
                    # `setdefault` in recovery keeps this newest match.
                    issue_comments = recovered_issue_comments(listed)
                candidates = issue_comments.get(part.part_number, [])
                existing = next(
                    (
                        comment
                        for comment in candidates
                        if comment.body == part.delivery.body
                    ),
                    candidates[0] if candidates else None,
                )
                if existing is not None:
                    if existing.body != part.delivery.body:
                        stale_code = stale_failure()
                        if stale_code is not None:
                            return terminalize_stale(stale_code)
                        prove_provider_write()
                        existing = github.update_issue_comment(
                            publication.repository,
                            existing.comment_id,
                            part.delivery.body,
                        )
                    external_id = existing.comment_id
                    recovered += 1
                else:
                    stale_code = stale_failure()
                    if stale_code is not None:
                        return terminalize_stale(stale_code)
                    prove_provider_write()
                    created = github.create_issue_comment(
                        publication.repository,
                        publication.pr_number,
                        part.delivery.body,
                    )
                    external_id = created.comment_id
                    issue_comments[part.part_number] = [created]
            else:
                if not isinstance(part.delivery, SuggestionReviewDelivery):
                    raise postgres_publications.PublicationConflict(
                        "stored suggestion part has the wrong delivery shape"
                    )
                expected = tuple(
                    InlineReviewComment(
                        path=comment.path,
                        body=comment.body,
                        line=comment.line,
                        side=comment.side.value,
                        start_line=comment.start_line,
                        start_side=(
                            comment.start_side.value
                            if comment.start_side is not None
                            else None
                        ),
                    )
                    for comment in part.delivery.comments
                )
                if review_comments is None:
                    review_comments = github.list_pull_request_review_comments(
                        publication.repository,
                        publication.pr_number,
                        max_pages=_COMMENT_RECOVERY_SCAN_PAGES,
                    )
                expected_signatures = Counter(
                    (
                        comment.body,
                        comment.path,
                        comment.line,
                        comment.side,
                        comment.start_line,
                        comment.start_side,
                    )
                    for comment in expected
                )
                comments_by_review: dict[int, list[PullRequestReviewComment]] = {}
                for comment in review_comments:
                    if (
                        comment.author_login.casefold() == author_login.casefold()
                        and comment.commit_id.lower() == publication.head_sha.lower()
                        and extract_publication_key(comment.body)
                        == publication.plan.publication_key
                    ):
                        comments_by_review.setdefault(comment.review_id, []).append(
                            comment
                        )
                exact_review_ids = sorted(
                    review_id
                    for review_id, comments in comments_by_review.items()
                    if Counter(
                        (
                            comment.body,
                            comment.path,
                            comment.line,
                            comment.side,
                            comment.start_line,
                            comment.start_side,
                        )
                        for comment in comments
                    )
                    == expected_signatures
                )
                if exact_review_ids:
                    external_id = exact_review_ids[-1]
                    recovered += 1
                else:
                    stale_code = stale_failure()
                    if stale_code is not None:
                        return terminalize_stale(stale_code)
                    prove_provider_write()
                    review = github.create_pull_request_review(
                        publication.repository,
                        publication.pr_number,
                        commit_id=publication.head_sha,
                        body=part.delivery.body,
                        comments=expected,
                    )
                    external_id = review.review_id
            acknowledge(part.part_type, part.part_number, external_id)
    except GitHubPublicationError as exc:
        with runtime.transaction() as connection:
            postgres_review_runs.lock_run(connection, publication.review_run_id)
            failed = postgres_publications.fail_publication(
                connection,
                publication_id=resolved_id,
                posting_started_at=posting_started_at,
                failure_code=exc.code,
                retryable=(
                    exc.status is None
                    or exc.status in {408, 425, 429}
                    or exc.status >= 500
                ),
                retry_delay=retry_delay,
                lease_owner=resolved_lease_owner,
                lease_generation=resolved_lease_generation,
            )
            if failed.status is PublicationStatus.FAILED:
                review_run_application.fail_run_after_publication_in_transaction(
                    connection,
                    publication.review_run_id,
                    failure_code=exc.code,
                    findings_count=findings_count,
                )
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=failed.status.value,
            published_parts=tuple(
                PostgresPublishedPart(part.part_type, part.external_id)
                for part in failed.parts
                if part.external_id is not None
            ),
            recovered_parts=recovered,
        )

    with runtime.transaction() as connection:
        postgres_review_runs.lock_run(connection, publication.review_run_id)
        posted = postgres_publications.complete_publication(
            connection,
            publication_id=resolved_id,
            posting_started_at=posting_started_at,
            lease_owner=resolved_lease_owner,
            lease_generation=resolved_lease_generation,
        )
        review_run_application.complete_run_after_publication_in_transaction(
            connection,
            publication.review_run_id,
            findings_count=findings_count,
        )
    supersession = _render_postgres_supersession(
        runtime,
        github=github,
        superseding_publication_id=publication_id,
        max_comment_bytes=max_comment_bytes,
    )
    _cleanup_postgres_failure_status(
        runtime,
        github=github,
        repository=publication.repository,
        pr_number=publication.pr_number,
    )
    return PostgresPublicationResult(
        publication_id=publication_id,
        status=posted.status.value,
        published_parts=tuple(
            PostgresPublishedPart(part.part_type, part.external_id)
            for part in posted.parts
            if part.external_id is not None
        ),
        recovered_parts=recovered,
        superseded_publication_id=(
            supersession.publication_id if supersession is not None else None
        ),
        supersession_rendered=(
            supersession.rendered if supersession is not None else None
        ),
        supersession_failure_code=(
            supersession.failure_code if supersession is not None else None
        ),
    )


def _comments_by_id(
    comments: Sequence[IssueComment], comment_ids: Sequence[int]
) -> list[IssueComment]:
    from .domain.publication import extract_publication_key

    indexed = {comment.comment_id: comment for comment in comments}
    return [
        indexed[comment_id]
        for comment_id in comment_ids
        if comment_id in indexed
        and extract_publication_key(indexed[comment_id].body) is not None
    ]


_FAILURE_STATUS_TOKEN = "review-agent:failure-status"
_FAILURE_STATUS_SCAN_PAGES = 50
_FAILURE_REASONS = {
    failure_codes.STALE_TIMEOUT: (
        "the review run stopped responding and was marked stale"
    ),
    failure_codes.GITHUB_DIFF_UNAVAILABLE: (
        "GitHub could not render this pull request's diff (it is very large)"
    ),
    failure_codes.REVIEW_DELIVER_ERROR: "the review failed during delivery",
    failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE: (
        "the review failed unexpectedly during delivery"
    ),
    failure_codes.JOB_RETRY_EXHAUSTED: (
        "the review worker exhausted its configured recovery attempts"
    ),
    failure_codes.JOB_EXECUTION_FAILED: (
        "the review worker encountered a non-retryable execution failure"
    ),
    failure_codes.PUBLICATION_ATTEMPTS_EXHAUSTED: (
        "the publisher exhausted its configured recovery attempts"
    ),
}


def _failure_status_marker(run_id: int, head_sha: str) -> str:
    return f"<!-- {_FAILURE_STATUS_TOKEN} run={run_id} head={head_sha} -->"


def _my_failure_status_comments(
    gateway: GitHubPublicationGateway, repository: str, pr_number: int
) -> list[IssueComment]:
    login = gateway.current_user_login().casefold()
    comments = gateway.list_issue_comments(
        repository,
        pr_number,
        max_pages=_FAILURE_STATUS_SCAN_PAGES,
    )
    return [
        comment
        for comment in comments
        if comment.author_login.casefold() == login
        and _FAILURE_STATUS_TOKEN in comment.body
    ]


def _failure_status_body(run_id: int, head_sha: str, failure_code: str) -> str:
    if failure_code == failure_codes.SNAPSHOT_SUPERSEDED:
        return (
            f"## {REVIEW_COMMENT_TITLE} — review snapshot was superseded\n\n"
            "The pull request base or head changed while this review was running, "
            "so no findings from the older snapshot were published.\n\n"
            "If a newer review is not already running, post `/review` as a new "
            "top-level PR comment after the latest changes are ready. Deterministic "
            "CI remains the merge gate.\n\n"
            f"- Status code: `{failure_code}`\n\n"
            f"{_failure_status_marker(run_id, head_sha)}\n"
        )
    reason = _FAILURE_REASONS.get(
        failure_code, "the review did not complete; see operator logs"
    )
    return (
        f"## {REVIEW_COMMENT_TITLE} — could not be completed\n\n"
        "This automated review did not finish, so no findings were published.\n\n"
        f"- Reason: {reason}\n"
        f"- Status code: `{failure_code}`\n\n"
        "This is an automated status, not a review result; deterministic CI remains "
        "the merge gate. After correcting the cause, post `/review` again as a new "
        "top-level PR comment. If it fails again, share the status code with the "
        "reviewer operator.\n\n"
        f"{_failure_status_marker(run_id, head_sha)}\n"
    )
