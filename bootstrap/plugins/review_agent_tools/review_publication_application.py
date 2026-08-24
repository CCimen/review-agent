"""Application orchestration for the existing publication lifecycle."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .postgres.runtime import PostgreSQLRuntime

try:
    from . import failure_codes, memory_publications, memory_runs, memory_suggestions
    from .domain.publication import PublicationDomainError
    from .github.publication import (
        GitHubPublicationError,
        GitHubPublicationGateway,
        InlineReviewComment,
        IssueComment,
        PullRequestReviewComment,
        PullRequestState,
        ambiguous_review_create_failure,
    )
    from .memory_validation import ReviewMemoryError, isoformat, utc_now
    from .publication_partition import (
        PublicationPart,
        extra_superseded_body,
        historical_bodies,
        publication_body_size,
        publication_parts_for_suggestion_state,
    )
    from .review_identity import REVIEW_COMMENT_TITLE
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import failure_codes  # type: ignore[no-redef]
    import memory_publications  # type: ignore[no-redef]
    import memory_runs  # type: ignore[no-redef]
    import memory_suggestions  # type: ignore[no-redef]
    from domain.publication import PublicationDomainError
    from github.publication import (
        GitHubPublicationError,
        GitHubPublicationGateway,
        InlineReviewComment,
        IssueComment,
        PullRequestReviewComment,
        PullRequestState,
        ambiguous_review_create_failure,
    )
    from memory_validation import ReviewMemoryError, isoformat, utc_now
    from publication_partition import (
        PublicationPart,
        extra_superseded_body,
        historical_bodies,
        publication_body_size,
        publication_parts_for_suggestion_state,
    )
    from review_identity import REVIEW_COMMENT_TITLE

_SUGGESTION_RECOVERY_SCAN_PAGES = 10


@dataclass(frozen=True, slots=True)
class PostgresPublicationResult:
    publication_id: int
    status: str
    external_ids: tuple[int, ...]
    recovered_parts: int


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


def publish_postgres_publication(
    runtime: "PostgreSQLRuntime",
    *,
    publication_id: int,
    github: GitHubPublicationGateway,
    recover_posting: bool = False,
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
    )
    from .postgres import publications as postgres_publications
    from .postgres import review_runs as postgres_review_runs

    resolved_id = PublicationId(publication_id)
    with runtime.transaction() as connection:
        claim = postgres_publications.claim_publication(connection, resolved_id)
    publication = claim.publication
    if publication.status is PublicationStatus.POSTED:
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=PublicationStatus.POSTED.value,
            external_ids=tuple(
                part.external_id
                for part in publication.parts
                if part.external_id is not None
            ),
            recovered_parts=0,
        )
    if not claim.acquired and not recover_posting:
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=PublicationStatus.POSTING.value,
            external_ids=tuple(
                part.external_id
                for part in publication.parts
                if part.external_id is not None
            ),
            recovered_parts=0,
        )
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
    issue_comments: dict[int, IssueComment] | None = None
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
    ) -> dict[int, IssueComment]:
        found: dict[int, IssueComment] = {}
        for comment in comments:
            if (
                comment.author_login.casefold() != author_login.casefold()
                or extract_publication_key(comment.body)
                != publication.plan.publication_key
            ):
                continue
            token = " part="
            token_index = comment.body.find(token)
            if token_index < 0:
                continue
            raw_number = comment.body[token_index + len(token) :].split("/", 1)[0]
            if not raw_number.isdigit() or int(raw_number) < 1:
                continue
            found.setdefault(int(raw_number), comment)
        return found

    def terminalize_stale(failure_code: str) -> PostgresPublicationResult:
        with runtime.transaction() as connection:
            postgres_publications.fail_publication(
                connection,
                publication_id=resolved_id,
                posting_started_at=posting_started_at,
                failure_code=failure_code,
                stale=True,
            )
            postgres_review_runs.fail_run(
                connection,
                publication.review_run_id,
                failure_code=failure_code,
                findings_count=findings_count,
            )
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=PublicationStatus.STALE.value,
            external_ids=(),
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
                        max_pages=_SUGGESTION_RECOVERY_SCAN_PAGES,
                        newest_first=True,
                    )
                    # `setdefault` in recovery keeps this newest match.
                    issue_comments = recovered_issue_comments(listed)
                existing = issue_comments.get(part.part_number)
                if existing is not None:
                    external_id = existing.comment_id
                    recovered += 1
                else:
                    stale_code = stale_failure()
                    if stale_code is not None:
                        return terminalize_stale(stale_code)
                    created = github.create_issue_comment(
                        publication.repository,
                        publication.pr_number,
                        part.delivery.body,
                    )
                    external_id = created.comment_id
                    issue_comments[part.part_number] = created
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
                        max_pages=_SUGGESTION_RECOVERY_SCAN_PAGES,
                    )
                matches = [
                    comment
                    for comment in review_comments
                    if comment.author_login.casefold() == author_login.casefold()
                    and comment.commit_id.lower() == publication.head_sha.lower()
                    and extract_publication_key(comment.body)
                    == publication.plan.publication_key
                    and any(
                        comment.body == candidate.body
                        and comment.path == candidate.path
                        and comment.line == candidate.line
                        and comment.side == candidate.side
                        and comment.start_line == candidate.start_line
                        and comment.start_side == candidate.start_side
                        for candidate in expected
                    )
                ]
                review_ids = {comment.review_id for comment in matches}
                if len(matches) == len(expected) and len(review_ids) == 1:
                    external_id = next(iter(review_ids))
                    recovered += 1
                else:
                    stale_code = stale_failure()
                    if stale_code is not None:
                        return terminalize_stale(stale_code)
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
            failed = postgres_publications.fail_publication(
                connection,
                publication_id=resolved_id,
                posting_started_at=posting_started_at,
                failure_code=exc.code,
            )
        return PostgresPublicationResult(
            publication_id=publication_id,
            status=failed.status.value,
            external_ids=tuple(
                part.external_id
                for part in failed.parts
                if part.external_id is not None
            ),
            recovered_parts=recovered,
        )

    with runtime.transaction() as connection:
        posted = postgres_publications.complete_publication(
            connection,
            publication_id=resolved_id,
            posting_started_at=posting_started_at,
        )
        postgres_review_runs.complete_run(
            connection,
            publication.review_run_id,
            findings_count=findings_count,
        )
    return PostgresPublicationResult(
        publication_id=publication_id,
        status=posted.status.value,
        external_ids=tuple(
            part.external_id for part in posted.parts if part.external_id is not None
        ),
        recovered_parts=recovered,
    )


def _verify_pr_target(
    publication: memory_publications.PublicationForPosting,
    pull: PullRequestState,
) -> str | None:
    if pull.state != "open":
        return "pr_not_open"
    if not publication["base_sha"]:
        return "missing_base_sha"
    if pull.base_sha != publication["base_sha"]:
        return "base_sha_changed"
    if pull.head_sha != publication["head_sha"]:
        return "head_sha_changed"
    return None


def _comments_by_author(
    comments: list[IssueComment], author_login: str
) -> list[IssueComment]:
    expected = author_login.casefold()
    return [
        comment
        for comment in comments
        if comment.author_login and comment.author_login.casefold() == expected
    ]


def _publication_comments(
    comments: list[IssueComment], publication_key: str
) -> dict[int, IssueComment]:
    found: dict[int, IssueComment] = {}
    for comment in comments:
        if memory_publications.extract_publication_key(comment.body) != publication_key:
            continue
        marker = memory_publications.publication_marker(publication_key)
        marker_index = comment.body.find(marker)
        part_number = 1
        part_token = " part="
        part_index = comment.body.find(part_token, marker_index)
        if part_index >= 0:
            raw_part = comment.body[part_index + len(part_token) :].split("/", 1)[0]
            try:
                part_number = int(raw_part)
            except ValueError:
                part_number = 1
        if part_number >= 1:
            found[part_number] = comment
    return found


def _comments_by_id(
    comments: list[IssueComment], comment_ids: list[int]
) -> list[IssueComment]:
    indexed = {comment.comment_id: comment for comment in comments}
    return [
        indexed[comment_id]
        for comment_id in comment_ids
        if comment_id in indexed
        and memory_publications.extract_publication_key(indexed[comment_id].body)
        is not None
    ]


def _publish_parts(
    *,
    github: GitHubPublicationGateway,
    repository: str,
    pr_number: int,
    parts: list[PublicationPart],
    existing_parts: dict[int, IssueComment],
) -> list[int]:
    posted_ids: list[int] = []
    for part in parts:
        target = existing_parts.get(part.part_number)
        if target is not None:
            comment = (
                target
                if target.body == part.body
                else github.update_issue_comment(
                    repository, target.comment_id, part.body
                )
            )
        else:
            comment = github.create_issue_comment(repository, pr_number, part.body)
        posted_ids.append(comment.comment_id)

    stale_candidates = [
        comment
        for part_number, comment in existing_parts.items()
        if part_number > len(parts) and comment.comment_id not in posted_ids
    ]
    deleted_ids: set[int] = set()
    for comment in stale_candidates:
        if comment.comment_id in deleted_ids:
            continue
        github.delete_issue_comment(repository, comment.comment_id)
        deleted_ids.add(comment.comment_id)
    return posted_ids


def _mark_supersession_failure(
    connection: sqlite3.Connection,
    publication_id: int,
    code: str,
) -> None:
    try:
        memory_publications.mark_supersession_rendered(
            connection, publication_id=publication_id, failure_code=code
        )
    except ReviewMemoryError:
        pass


def _render_superseded_publication(
    connection: sqlite3.Connection,
    *,
    github: GitHubPublicationGateway,
    comments: list[IssueComment],
    superseding_publication_id: int,
    max_comment_bytes: int,
) -> dict[str, object] | None:
    try:
        publication = memory_publications.publication_for_supersession(
            connection, superseding_publication_id
        )
    except ReviewMemoryError:
        return {
            "supersession_rendered": False,
            "supersession_failure_code": "supersession_lookup_failed",
        }
    if publication is None:
        return None
    targets = _comments_by_id(comments, publication["comment_ids"])
    if len(targets) != len(publication["comment_ids"]):
        _mark_supersession_failure(
            connection, publication["publication_id"], "superseded_comment_missing"
        )
        return {
            "superseded_publication_id": publication["publication_id"],
            "supersession_rendered": False,
            "supersession_failure_code": "superseded_comment_missing",
        }
    try:
        parts = historical_bodies(
            publication,
            max_comment_bytes=max_comment_bytes,
            target_parts=len(targets),
        )
        if len(parts) > len(targets):
            raise GitHubPublicationError("superseded_body_needs_more_parts")
        for index, target in enumerate(targets):
            if index < len(parts):
                body = parts[index].body
            else:
                body = extra_superseded_body(
                    publication,
                    part_number=index + 1,
                    total_parts=len(targets),
                )
            if target.body != body:
                github.update_issue_comment(
                    publication["repository"], target.comment_id, body
                )
    except (GitHubPublicationError, PublicationDomainError, ValueError) as exc:
        code = (
            exc.code
            if isinstance(exc, (GitHubPublicationError, PublicationDomainError))
            else "supersession_failed"
        )
        _mark_supersession_failure(connection, publication["publication_id"], code)
        return {
            "superseded_publication_id": publication["publication_id"],
            "supersession_rendered": False,
            "supersession_failure_code": code,
        }
    memory_publications.mark_supersession_rendered(
        connection, publication_id=publication["publication_id"]
    )
    return {
        "superseded_publication_id": publication["publication_id"],
        "supersession_rendered": True,
    }


def _suggestion_review_body(review_number: int | None, count: int) -> str:
    label = f"Review {review_number}" if review_number is not None else "this review"
    patch_label = "patch" if count == 1 else "patches"
    return (
        f"## Optional atomic patches · {label}\n\n"
        f"GitHub grouped {count} proposed atomic {patch_label} here so each can be "
        "inspected in context. Apply a patch only after confirming it fits the "
        "surrounding invariants, or add selected suggestions to a batch. Run the "
        "relevant checks, push the result, then post `/review` again. Applying a "
        "patch does not itself mark the finding resolved."
    )


def _inline_suggestion_body(
    suggestion: memory_suggestions.PublicationSuggestion,
) -> str:
    replacement = suggestion["replacement_text"]
    return (
        f"**{suggestion['local_reference']} · Optional atomic patch**\n\n"
        "This is a small patch candidate intended to stand on its own. Confirm it "
        "fits the surrounding invariants and run the relevant checks after applying "
        "it.\n\n"
        "```suggestion\n"
        f"{replacement}\n"
        "```\n\n"
        f"{memory_suggestions.suggestion_marker(suggestion['suggestion_key'])}"
    )


def _inline_suggestion_comment(
    suggestion: memory_suggestions.PublicationSuggestion,
) -> InlineReviewComment:
    multiline = suggestion["start_line"] != suggestion["end_line"]
    return InlineReviewComment(
        path=suggestion["path"],
        body=_inline_suggestion_body(suggestion),
        line=suggestion["end_line"],
        side="RIGHT",
        start_line=suggestion["start_line"] if multiline else None,
        start_side="RIGHT" if multiline else None,
    )


def _recovered_suggestion_comments(
    comments: Sequence[PullRequestReviewComment],
    *,
    author_login: str,
    head_sha: str,
    suggestions: Sequence[memory_suggestions.PublicationSuggestion],
) -> dict[str, PullRequestReviewComment]:
    expected_author = author_login.casefold()
    expected = {item["suggestion_key"]: item for item in suggestions}
    recovered: dict[str, PullRequestReviewComment] = {}
    for comment in comments:
        if comment.author_login.casefold() != expected_author:
            continue
        key = memory_suggestions.extract_suggestion_key(comment.body)
        suggestion = expected.get(key or "")
        if suggestion is None or comment.commit_id.lower() != head_sha.lower():
            continue
        expected_start = (
            suggestion["start_line"]
            if suggestion["start_line"] != suggestion["end_line"]
            else None
        )
        if (
            comment.path != suggestion["path"]
            or comment.line != suggestion["end_line"]
            or comment.side != "RIGHT"
            or comment.start_line != expected_start
            or comment.start_side != ("RIGHT" if expected_start is not None else None)
        ):
            continue
        recovered.setdefault(suggestion["suggestion_key"], comment)
    return recovered


def _publish_suggestions(
    connection: sqlite3.Connection,
    *,
    publication: memory_publications.PublicationForPosting,
    github: GitHubPublicationGateway,
) -> dict[str, object]:
    suggestions = memory_suggestions.suggestions_for_publication(
        connection, publication["publication_id"]
    )[: memory_suggestions.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW]
    if not suggestions:
        return {
            "suggestions_published": False,
            "suggestions_count": 0,
            "suggestion_delivery_status": "none",
        }

    claim = memory_suggestions.claim_suggestions_for_posting(
        connection, publication["publication_id"]
    )
    if claim["suggestion_delivery_status"] == "posted":
        return {
            "suggestions_published": True,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": "posted",
            "suggestion_review_id": claim["suggestion_review_id"],
            "suggestions_idempotent": True,
        }
    if not claim["claimed"]:
        return {
            "suggestions_published": False,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": claim["suggestion_delivery_status"],
            "suggestion_failure_code": claim["suggestion_failure_code"],
        }

    claim_started_at = claim["suggestion_posting_started_at"]
    try:
        if claim_started_at is None:
            raise ReviewMemoryError("suggestion delivery claim has no lease timestamp")
        author_login = github.current_user_login()
        review_comments = github.list_pull_request_review_comments(
            publication["repository"],
            publication["pr_number"],
            max_pages=_SUGGESTION_RECOVERY_SCAN_PAGES,
        )
        recovered = _recovered_suggestion_comments(
            review_comments,
            author_login=author_login,
            head_sha=publication["head_sha"],
            suggestions=suggestions,
        )
        missing = [
            item for item in suggestions if item["suggestion_key"] not in recovered
        ]
        if missing:
            stale_code = _verify_pr_target(
                publication,
                github.get_pull_request(
                    publication["repository"], publication["pr_number"]
                ),
            )
            if stale_code:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication["publication_id"],
                    failure_code=stale_code,
                    stale=True,
                    claim_started_at=claim_started_at,
                )
                return {
                    "suggestions_published": False,
                    "suggestions_count": len(suggestions),
                    "suggestion_delivery_status": "stale",
                    "suggestion_failure_code": stale_code,
                }
            claim_started_at = memory_suggestions.renew_suggestion_claim(
                connection,
                publication_id=publication["publication_id"],
                claim_started_at=claim_started_at,
            )
            try:
                review = github.create_pull_request_review(
                    publication["repository"],
                    publication["pr_number"],
                    commit_id=publication["head_sha"],
                    body=_suggestion_review_body(
                        publication["review_number"], len(missing)
                    ),
                    comments=tuple(
                        _inline_suggestion_comment(item) for item in missing
                    ),
                )
                review_id = review.review_id
            except GitHubPublicationError as exc:
                if ambiguous_review_create_failure(exc):
                    reconciled_comments = github.list_pull_request_review_comments(
                        publication["repository"],
                        publication["pr_number"],
                        max_pages=_SUGGESTION_RECOVERY_SCAN_PAGES,
                    )
                    reconciled = _recovered_suggestion_comments(
                        reconciled_comments,
                        author_login=author_login,
                        head_sha=publication["head_sha"],
                        suggestions=suggestions,
                    )
                    if len(reconciled) == len(suggestions):
                        review_id = max(
                            comment.review_id for comment in reconciled.values()
                        )
                    else:
                        raise
                else:
                    raise
        else:
            review_id = max(comment.review_id for comment in recovered.values())
        claim_started_at = memory_suggestions.renew_suggestion_claim(
            connection,
            publication_id=publication["publication_id"],
            claim_started_at=claim_started_at,
        )
        memory_suggestions.mark_suggestions_posted(
            connection,
            publication_id=publication["publication_id"],
            review_id=review_id,
            claim_started_at=claim_started_at,
        )
        return {
            "suggestions_published": True,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": "posted",
            "suggestion_review_id": review_id,
            "suggestions_recovered": len(recovered),
            "suggestions_created": len(missing),
        }
    except (GitHubPublicationError, ReviewMemoryError) as exc:
        failure_code = (
            exc.code
            if isinstance(exc, GitHubPublicationError)
            else "suggestion_state_failed"
        )
        if claim_started_at is not None:
            try:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication["publication_id"],
                    failure_code=failure_code,
                    claim_started_at=claim_started_at,
                )
            except ReviewMemoryError:
                delivery = memory_suggestions.suggestion_delivery_status(
                    connection, publication["publication_id"]
                )
                state = delivery["suggestion_delivery_status"]
                result: dict[str, object] = {
                    "suggestions_published": state == "posted",
                    "suggestions_count": len(suggestions),
                    "suggestion_delivery_status": state,
                }
                if delivery["suggestion_review_id"] is not None:
                    result["suggestion_review_id"] = delivery["suggestion_review_id"]
                if delivery["suggestion_failure_code"]:
                    result["suggestion_failure_code"] = delivery[
                        "suggestion_failure_code"
                    ]
                return result
        return {
            "suggestions_published": False,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": "publish_failed",
            "suggestion_failure_code": failure_code,
        }


def publish_review(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int,
    github: GitHubPublicationGateway,
    max_comment_bytes: int,
) -> dict[str, object]:
    publication = memory_publications.claim_publication_for_posting(
        connection, publication_id=publication_id, review_run_id=review_run_id
    )
    already_posted = publication["delivery_status"] == "posted"
    budget = max_comment_bytes

    try:
        stale_code = _verify_pr_target(
            publication,
            github.get_pull_request(
                publication["repository"], publication["pr_number"]
            ),
        )
        if stale_code:
            suggestion_delivery = memory_suggestions.suggestion_delivery_status(
                connection, publication_id
            )
            if suggestion_delivery["suggestion_delivery_status"] in {
                "pending",
                "posting",
                "posted",
                "publish_failed",
            }:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication_id,
                    failure_code=stale_code,
                    stale=True,
                )
            if already_posted:
                return {
                    "published": True,
                    "publication_id": publication["publication_id"],
                    "comment_id": publication["comment_id"],
                    "delivery_status": "posted",
                    "idempotent": True,
                    "suggestions_published": False,
                    "suggestion_delivery_status": "stale",
                    "suggestion_failure_code": stale_code,
                }
            memory_publications.mark_publication_failed(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                failure_code=stale_code,
                status="stale",
            )
            return {
                "published": False,
                "publication_id": publication_id,
                "delivery_status": "stale",
                "failure_code": stale_code,
            }

        suggestion_result = _publish_suggestions(
            connection, publication=publication, github=github
        )
        try:
            parts = publication_parts_for_suggestion_state(
                publication,
                suggestions_published=bool(
                    suggestion_result.get("suggestions_published", False)
                ),
                max_comment_bytes=budget,
            )
        except (GitHubPublicationError, PublicationDomainError) as exc:
            if already_posted:
                result: dict[str, object] = {
                    "published": True,
                    "publication_id": publication["publication_id"],
                    "comment_id": publication["comment_id"],
                    "delivery_status": "posted",
                    "idempotent": True,
                    "summary_refresh_failure_code": exc.code,
                }
                result.update(suggestion_result)
                return result
            memory_publications.mark_publication_failed(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                failure_code=exc.code,
            )
            result = {
                "published": False,
                "publication_id": publication_id,
                "delivery_status": "publish_failed",
                "failure_code": exc.code,
                "body_bytes": publication_body_size(publication["rendered_markdown"]),
                "max_comment_bytes": budget,
            }
            result.update(suggestion_result)
            return result

        comments = _comments_by_author(
            github.list_issue_comments(
                publication["repository"], publication["pr_number"]
            ),
            github.current_user_login(),
        )
        current_parts = _publication_comments(comments, publication["publication_key"])
        stale_code = _verify_pr_target(
            publication,
            github.get_pull_request(
                publication["repository"], publication["pr_number"]
            ),
        )
        if stale_code:
            suggestion_delivery = memory_suggestions.suggestion_delivery_status(
                connection, publication_id
            )
            if suggestion_delivery["suggestion_delivery_status"] not in {
                "none",
                "stale",
            }:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication_id,
                    failure_code=stale_code,
                    stale=True,
                )
            if already_posted:
                return {
                    "published": True,
                    "publication_id": publication["publication_id"],
                    "comment_id": publication["comment_id"],
                    "delivery_status": "posted",
                    "idempotent": True,
                    "suggestions_published": False,
                    "suggestion_delivery_status": "stale",
                    "suggestion_failure_code": stale_code,
                }
            memory_publications.mark_publication_failed(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                failure_code=stale_code,
                status="stale",
            )
            return {
                "published": False,
                "publication_id": publication_id,
                "delivery_status": "stale",
                "failure_code": stale_code,
            }
        if already_posted:
            comment_ids = (
                _publish_parts(
                    github=github,
                    repository=publication["repository"],
                    pr_number=publication["pr_number"],
                    parts=parts,
                    existing_parts=current_parts,
                )
                if current_parts
                else [int(publication["comment_id"] or 0)]
            )
            result = {
                "published": True,
                "publication_id": publication["publication_id"],
                "comment_id": publication["comment_id"],
                "comment_ids": [value for value in comment_ids if value > 0],
                "delivery_status": "posted",
                "idempotent": True,
                "summary_refreshed": bool(current_parts),
            }
            result.update(suggestion_result)
            return result

        if current_parts:
            comment_ids = _publish_parts(
                github=github,
                repository=publication["repository"],
                pr_number=publication["pr_number"],
                parts=parts,
                existing_parts=current_parts,
            )
            posted = memory_publications.mark_publication_posted(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                comment_id=comment_ids[0],
                comment_ids=comment_ids,
            )
            _cleanup_prior_failure_status(
                connection, github, publication["repository"], publication["pr_number"]
            )
            result = {
                "published": True,
                "publication_id": publication_id,
                "comment_id": comment_ids[0],
                "comment_ids": comment_ids,
                "parts": len(comment_ids),
                "delivery_status": posted["delivery_status"],
                "recovered": True,
            }
            result.update(suggestion_result)
            return result

        comment_ids = _publish_parts(
            github=github,
            repository=publication["repository"],
            pr_number=publication["pr_number"],
            parts=parts,
            existing_parts={},
        )

        posted = memory_publications.mark_publication_posted(
            connection,
            publication_id=publication_id,
            review_run_id=review_run_id,
            comment_id=comment_ids[0],
            comment_ids=comment_ids,
        )
        supersession = _render_superseded_publication(
            connection,
            github=github,
            comments=comments,
            superseding_publication_id=publication_id,
            max_comment_bytes=budget,
        )
        _cleanup_prior_failure_status(
            connection, github, publication["repository"], publication["pr_number"]
        )
        result: dict[str, object] = {
            "published": True,
            "publication_id": publication_id,
            "comment_id": comment_ids[0],
            "comment_ids": comment_ids,
            "parts": len(comment_ids),
            "delivery_status": posted["delivery_status"],
            "recovered": False,
        }
        if supersession is not None:
            result.update(supersession)
        result.update(suggestion_result)
        return result
    except GitHubPublicationError as exc:
        if already_posted:
            return {
                "published": True,
                "publication_id": publication["publication_id"],
                "comment_id": publication["comment_id"],
                "delivery_status": "posted",
                "idempotent": True,
                "summary_refresh_failure_code": exc.code,
            }
        memory_publications.mark_publication_failed(
            connection,
            publication_id=publication_id,
            review_run_id=review_run_id,
            failure_code=exc.code,
        )
        return {
            "published": False,
            "publication_id": publication_id,
            "delivery_status": "publish_failed",
            "failure_code": exc.code,
        }


def _cleanup_prior_failure_status(
    connection: sqlite3.Connection,
    github: GitHubPublicationGateway,
    repository: str,
    pr_number: int,
) -> None:
    """Remove any failure-status comments once a real review has posted for this PR.

    Stored-comment-id first; a comment that is already gone is tolerated (the stored id
    is cleared regardless so it is not retried). A deep marker scan then removes any
    failure-status comments that were posted but never had their id durably stored (e.g.
    a pre-migration/degraded post), which the stored-id sweep cannot see."""
    deleted: set[int] = set()
    for entry in memory_runs.failure_status_comments_for_pr(
        connection, repository, pr_number
    ):
        comment_id = int(entry["comment_id"])
        try:
            github.delete_issue_comment(repository, comment_id)
        except GitHubPublicationError:
            pass
        deleted.add(comment_id)
        memory_runs.clear_failure_status_comment(connection, entry["run_id"])
    for comment in _my_failure_status_comments(github, repository, pr_number):
        if comment.comment_id in deleted:
            continue
        try:
            github.delete_issue_comment(repository, comment.comment_id)
        except GitHubPublicationError:
            pass
        deleted.add(comment.comment_id)

_FAILURE_STATUS_TOKEN = "review-agent:failure-status"
# The failure-status fallback (no stored comment id) must find a recent comment even on
# very noisy PRs. GitHub returns issue comments oldest-first, so a recently posted
# failure-status comment can sit far beyond the default 300-comment window; scan deeper
# (bounded) on this rare fallback path so we never duplicate or orphan one.
_FAILURE_STATUS_SCAN_PAGES = 50
_FAILURE_REASONS = {
    failure_codes.STALE_TIMEOUT: (
        "the review run stopped responding and was marked stale"
    ),
    "github_diff_406": "GitHub could not render this pull request's diff (it is very large)",
    failure_codes.REVIEW_DELIVER_ERROR: "the review failed during delivery",
    failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE: (
        "the review failed unexpectedly during delivery"
    ),
}


def _failure_status_marker(run_id: int, head_sha: str) -> str:
    return f"<!-- {_FAILURE_STATUS_TOKEN} run={run_id} head={head_sha} -->"


def _my_failure_status_comments(
    gateway: GitHubPublicationGateway, repository: str, pr_number: int
) -> list[IssueComment]:
    """Deep-scan the bot's own failure-status comments (beyond the 300-comment cap)."""
    mine = _comments_by_author(
        gateway.list_issue_comments(
            repository, pr_number, max_pages=_FAILURE_STATUS_SCAN_PAGES
        ),
        gateway.current_user_login(),
    )
    return [comment for comment in mine if _FAILURE_STATUS_TOKEN in comment.body]


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


def _persist_failure_status(
    connection: sqlite3.Connection, run_id: int, comment_id: int
) -> str:
    posted_at = isoformat(utc_now())
    try:
        memory_runs.record_failure_status_comment(
            connection, run_id, comment_id=comment_id, posted_at=posted_at
        )
    except sqlite3.OperationalError:
        # Pre-migration database without the durable columns: the comment is posted;
        # we just cannot store its id. connect() normally migrates before serving.
        pass
    return posted_at


def publish_run_failure_status(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    failure_code: str,
    github: GitHubPublicationGateway,
) -> dict[str, object]:
    """Post (or idempotently update) a deterministic 'review could not complete' comment.

    Stored-comment-id first: if the run already has a failure_status_comment_id, PATCH it
    directly without listing comments (robust on PRs with hundreds of comments). Otherwise
    search the bot's own comments for the failure-status marker, else create a new comment.
    Works on terminal status='failed' rows. The body is deterministic code, never model
    text, satisfying the "no model-authored fallback comment" rule.
    """
    run = memory_runs.get_run(connection, run_id)
    if run is None:
        raise ReviewMemoryError("run_id is not a known review run")
    repository = str(run["repository"])
    pr_number = int(run["pr_number"])
    head_sha = str(run.get("head_sha") or "")
    stored_id = run.get("failure_status_comment_id")
    gateway = github
    body = _failure_status_body(int(run_id), head_sha, failure_code)

    if isinstance(stored_id, int) and not isinstance(stored_id, bool) and stored_id > 0:
        comment = gateway.update_issue_comment(repository, stored_id, body)
    else:
        marker = _failure_status_marker(int(run_id), head_sha)
        existing = next(
            (
                comment
                for comment in _my_failure_status_comments(
                    gateway, repository, pr_number
                )
                if marker in comment.body
            ),
            None,
        )
        if existing is not None:
            comment = gateway.update_issue_comment(
                repository, existing.comment_id, body
            )
        else:
            comment = gateway.create_issue_comment(repository, pr_number, body)

    posted_at = _persist_failure_status(connection, int(run_id), comment.comment_id)
    return {
        "posted": True,
        "run_id": int(run_id),
        "comment_id": comment.comment_id,
        "failure_code": failure_code,
        "posted_at": posted_at,
    }
