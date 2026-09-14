"""Deterministic documentation scope before a leased worker invokes Hermes."""

from __future__ import annotations

from dataclasses import replace

from . import (
    capacity,
    documentation_scope,
    repository_decision_context,
    repository_guidance_context,
    review_contract,
    review_run_application,
)
from .domain.documentation_policy import DocumentationPolicyError, parse_policy
from .domain.documentation_review import DocumentationOutcome, DocumentationResult
from .domain.review import ReviewPurpose, ReviewRunId
from .github.gateway import GitHubGatewayRejected
from .github.source import ReviewPolicySource
from .postgres import coverage, documentation_admissions, documentation_reviews, github_app, jobs, review_runs
from .postgres.runtime import PostgreSQLRuntime
from .review_source_initialization import initialize_review
from .review_tool_runtime import GatewaySourceSession, ToolInputError, output_json


def policy_scope(
    *,
    base_sha: str,
    comparison_sha: str | None,
    head_sha: str,
    changed_files: tuple[documentation_scope.ChangedPath, ...],
    inventory_complete: bool,
    base: ReviewPolicySource,
    head: ReviewPolicySource,
) -> documentation_scope.DocumentationScope:
    """Use accepted target-base rules; retain a head proposal without activating it."""
    if base.revision != base_sha or head.revision != head_sha:
        raise ToolInputError("documentation policy does not match the review snapshot")
    proposal_status = "unchanged"
    proposal_detail = None
    proposed = None
    if head.state == "not_found_at_revision":
        if base.state != "not_found_at_revision":
            proposal_status = "removed"
    elif head.state != "ok":
        proposal_status, proposal_detail = "unavailable", head.state
    elif head.content_sha256 != base.content_sha256:
        try:
            proposed = parse_policy(head.content or "")
            proposal_status = "valid"
        except DocumentationPolicyError as exc:
            proposal_status, proposal_detail = "invalid", str(exc)

    policy = None
    failure = None
    if base.state == "ok":
        try:
            policy = parse_policy(base.content or "")
        except DocumentationPolicyError as exc:
            failure = str(exc)
    elif base.state != "not_found_at_revision":
        failure = f"accepted_policy_{base.state}"

    preview = policy is None and failure is None and proposed is not None
    digest = head.content_sha256 if preview else base.content_sha256
    result = documentation_scope.plan_scope(
        base_sha=base_sha, comparison_sha=comparison_sha, head_sha=head_sha,
        changed_files=changed_files, policy=proposed if preview else policy,
        policy_hash=f"sha256:{digest}" if digest else None,
        proposal_status=proposal_status, proposal_detail=proposal_detail,
    )
    reasons: list[str] = []
    if not inventory_complete:
        reasons.append("changed_file_inventory_incomplete")
    if comparison_sha is None:
        reasons.append("comparison_base_unavailable")
    if failure is not None:
        reasons.append(failure)
        result = replace(result, status="invalid_configuration", active_policy=False)
    elif preview:
        result = replace(result, status="not_configured", active_policy=False)
    return replace(result, incomplete_reasons=tuple(reasons))


def deterministic_outcome(
    scope: documentation_scope.DocumentationScope,
) -> DocumentationOutcome | None:
    if scope.status == "not_configured":
        return DocumentationOutcome.NOT_CONFIGURED
    if scope.status == "invalid_configuration":
        return DocumentationOutcome.INVALID_CONFIGURATION
    if scope.comparison_sha is None:
        return DocumentationOutcome.UNAVAILABLE
    if not scope.incomplete_reasons and not scope.areas and not scope.unmapped_paths:
        return DocumentationOutcome.NOT_NEEDED
    return None


def run_preflight(
    runtime: PostgreSQLRuntime,
    source: GatewaySourceSession,
    installed_contract: review_contract.ReviewContract,
) -> DocumentationResult:
    """Freeze scope or a zero-model result under the same worker lease as inference."""
    run_id = ReviewRunId(source.run_id)
    with runtime.transaction() as connection:
        run_scope = review_runs.get_run_scope(connection, run_id)
        if run_scope.run.purpose is not ReviewPurpose.DOCUMENTATION:
            raise ToolInputError("documentation preflight requires a documentation review")
        try:
            github_app.authorize_documentation_review(
                connection, run_scope.provider_repository_id,
                automatic=documentation_admissions.is_automatic(connection, run_id),
            )
        except github_app.GitHubAppRepositoryUnauthorized as exc:
            raise ToolInputError("documentation review is no longer enabled for this request") from exc
        existing = documentation_reviews.get_result(connection, run_id=run_id)
    initialized = initialize_review(source, runtime, installed_contract)
    if existing is not None and existing.frozen:
        return existing
    if existing is None:
        try:
            source.client.get_documentation_comparison(
                run_id=source.run_id, job_id=source.lease.job_id,
                lease_generation=source.lease.lease_generation,
            )
        except GitHubGatewayRejected as exc:
            if exc.reason != "documentation_comparison_unavailable":
                raise
            with runtime.transaction() as connection:
                review_runs.lock_run(connection, run_id)
                jobs.require_live_lease(connection, job_id=source.lease.job_id,
                    review_run_id=run_id, lease_generation=source.lease.lease_generation)
                documentation_reviews.initialize(connection, run_id=run_id, comparison_sha=None)
    with runtime.transaction() as connection:
        result = documentation_reviews.get_result(connection, run_id=run_id)
    if result is None:
        raise ToolInputError("documentation comparison was not recorded")
    if result.scope is None:
        files: list[documentation_scope.ChangedPath] = []
        cursor = ""
        while True:
            with runtime.transaction() as connection:
                page = coverage.list_run_files(
                    connection, run_id=run_id, repository=run_scope.repository,
                    pr_number=run_scope.pr_number, limit=200, cursor=cursor,
                )
            files.extend(documentation_scope.ChangedPath(
                item.change_status, item.path, item.previous_path or None,
            ) for item in page.items)
            if len(files) > documentation_scope.MAX_CHANGED_FILES:
                raise ToolInputError("documentation inventory exceeds the provider limit")
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        base = source.client.get_documentation_policy(
            run_id=source.run_id, job_id=source.lease.job_id,
            lease_generation=source.lease.lease_generation, side="base",
        )
        head = source.client.get_documentation_policy(
            run_id=source.run_id, job_id=source.lease.job_id,
            lease_generation=source.lease.lease_generation, side="head",
        )
        scope = policy_scope(
            base_sha=run_scope.base_sha, comparison_sha=result.comparison_sha,
            head_sha=run_scope.head_sha, changed_files=tuple(files),
            inventory_complete=initialized.file_index.registration_complete,
            base=base, head=head,
        )
        with runtime.transaction() as connection:
            review_runs.lock_run(connection, run_id)
            jobs.require_live_lease(connection, job_id=source.lease.job_id,
                review_run_id=run_id, lease_generation=source.lease.lease_generation)
            result = documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope)
    assert result.scope is not None
    outcome = deterministic_outcome(result.scope)
    if outcome is not None:
        with runtime.transaction() as connection:
            review_runs.lock_run(connection, run_id)
            jobs.require_live_lease(connection, job_id=source.lease.job_id,
                review_run_id=run_id, lease_generation=source.lease.lease_generation)
            return documentation_reviews.finalize(
                connection, run_id=run_id, outcome=outcome, semantic_inference_used=False,
            )

    def load_guidance() -> repository_guidance_context.RepositoryGuidanceContext:
        context = repository_guidance_context.load(
            source, repository=run_scope.repository, base_sha=run_scope.base_sha,
            content_max_chars=capacity.current().text_page_max_chars,
        )
        if len(output_json(repository_guidance_context.payload(context))) > capacity.current().result_max_chars // 3:
            return repository_guidance_context.failed(
                "unavailable", base_sha=run_scope.base_sha, config_hash=context.config_hash,
                failure_code="guidance_result_budget",
            )
        return context

    def load_decisions(paths: tuple[str, ...]) -> repository_decision_context.RepositoryDecisionContext:
        if not initialized.file_index.registration_complete:
            return repository_decision_context.unavailable(
                base_sha=run_scope.base_sha, failure_code="decision_changed_file_index_incomplete",
            )
        context = repository_decision_context.load(
            source, repository=run_scope.repository, base_sha=run_scope.base_sha,
            changed_paths=paths,
        )
        if len(output_json(repository_decision_context.payload(context))) > capacity.current().result_max_chars // 3:
            return repository_decision_context.unavailable(
                base_sha=run_scope.base_sha, failure_code="decision_context_result_budget",
            )
        return context

    review_run_application.load_or_create_live_repository_guidance(
        runtime, initialized.subject, loader=load_guidance,
    )
    review_run_application.load_or_create_live_repository_decisions(
        runtime, initialized.subject, loader=load_decisions,
    )
    return result
