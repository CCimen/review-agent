"""Bounded documentation assessment tools for an already admitted worker run."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from . import capacity, repository_decision_context, repository_guidance_context, settings
from .domain.documentation_review import DocumentationResult, DocumentationReviewError
from .domain.review import ReviewPurpose, ReviewRunId
from .github.gateway import GitHubGatewayError
from .postgres import documentation_reviews, repository_decisions, repository_guidance, review_runs
from .postgres.runtime import PostgreSQLRuntimeError
from .review_source_tools import page_output
from .review_tool_runtime import (
    GatewaySourceSession, ReviewRunTerminal, ToolInputError, error_output,
    gateway_source_session, output_json, postgres_runtime, review_run_snapshot,
    run_terminal_payload, worker_lease_fence,
)


def _result(source: GatewaySourceSession) -> DocumentationResult:
    with postgres_runtime().transaction() as connection:
        run = review_runs.get_run(connection, ReviewRunId(source.run_id))
        if run.purpose is not ReviewPurpose.DOCUMENTATION:
            raise ToolInputError("this tool requires a documentation review")
        result = documentation_reviews.get_result(connection, run_id=ReviewRunId(source.run_id))
    if result is None or result.scope is None:
        raise ToolInputError("the worker has not prepared documentation scope")
    return result


def _integer(args: dict[str, Any], name: str, default: int, maximum: int) -> int:
    value = args.get(name, default)
    if type(value) is not int or value < 1 or value > maximum:
        raise ToolInputError(f"{name} must be between 1 and {maximum}")
    return value


@worker_lease_fence()
def docs_begin(args: dict[str, Any], **context: Any) -> str:
    try:
        source = gateway_source_session(args, context)
        result = _result(source)
        scope = result.scope
        assert scope is not None
        with postgres_runtime().transaction() as connection:
            identity = review_runs.get_run_scope(connection, ReviewRunId(source.run_id))
            guidance = repository_guidance.load_context(connection, run_id=ReviewRunId(source.run_id))
            decisions = repository_decisions.load_context(connection, run_id=ReviewRunId(source.run_id))
            previous = documentation_reviews.previous_findings(connection, run_id=ReviewRunId(source.run_id))
        if not result.frozen:
            review_run_snapshot(source=source, repository=identity.repository,
                pr_number=identity.pr_number, phase="reviewing", expected_head_sha=result.head_sha)
        return page_output({
            "run_id": source.run_id, "purpose": "documentation", "frozen": result.frozen,
            "outcome": result.outcome, "base_sha": result.base_sha,
            "comparison_sha": result.comparison_sha, "head_sha": result.head_sha,
            "policy_hash": scope.policy_hash, "proposal_status": scope.proposal_status,
            "proposal_detail": scope.proposal_detail,
            "counts": {"areas": len(scope.areas), "unmapped": len(scope.unmapped_paths),
                       "exclusions": len(scope.exclusions), "documents": len(scope.documents),
                       "previous_findings": len(previous)},
            "incomplete_reasons": list(scope.incomplete_reasons),
            "repository_guidance_untrusted": repository_guidance_context.payload(guidance),
            "repository_decisions_untrusted": repository_decision_context.payload(decisions),
            "next_action": "Page selected areas, unmapped paths, and previous findings with review_agent_docs_scope; read exact evidence with review_agent_docs_file.",
        })
    except ReviewRunTerminal as terminal:
        return output_json(run_terminal_payload(terminal.run_id))
    except (ValueError, GitHubGatewayError, PostgreSQLRuntimeError) as exc:
        return error_output(str(exc))


@worker_lease_fence()
def docs_scope(args: dict[str, Any], **context: Any) -> str:
    try:
        source = gateway_source_session(args, context)
        result = _result(source)
        assert result.scope is not None
        section = args.get("section", "areas")
        if section == "areas":
            items: list[object] = [item.to_json_obj() for item in result.scope.areas]
        elif section == "unmapped":
            items = list(result.scope.unmapped_paths)
        elif section == "exclusions":
            items = [item.to_json_obj() for item in result.scope.exclusions]
        elif section == "previous_findings":
            with postgres_runtime().transaction() as connection:
                previous = documentation_reviews.previous_findings(connection, run_id=ReviewRunId(source.run_id))
            items = [asdict(item) for item in previous]
        else:
            raise ToolInputError("section must be areas, unmapped, exclusions, or previous_findings")
        page = _integer(args, "page", 1, 3000)
        limit = _integer(args, "limit", 5, 10)
        offset = (page - 1) * limit
        return page_output({"section": section, "page": page, "total": len(items),
            "items_untrusted": items[offset:offset + limit],
            "next_page": page + 1 if offset + limit < len(items) else None})
    except (ValueError, GitHubGatewayError, PostgreSQLRuntimeError) as exc:
        return error_output(str(exc))


@worker_lease_fence()
def docs_file(args: dict[str, Any], **context: Any) -> str:
    try:
        source = gateway_source_session(args, context)
        result = _result(source)
        if result.frozen:
            raise DocumentationReviewError("documentation evidence is frozen")
        path = args.get("path")
        role = args.get("role", "head")
        if not isinstance(path, str) or role not in {"head", "comparison", "policy"}:
            raise ToolInputError("path and an exact head, comparison, or policy role are required")
        start = _integer(args, "start_line", 1, 2_000_000)
        count = _integer(args, "max_lines", 200, 400)
        page = source.client.get_review_file_page(
            run_id=source.run_id, job_id=source.lease.job_id,
            lease_generation=source.lease.lease_generation, path=path,
            side="base" if role == "policy" else str(role),
            start_line=start, max_lines=count, max_chars=capacity.current().text_page_max_chars,
            purpose=ReviewPurpose.DOCUMENTATION,
        )
        return page_output({
            **page.to_mapping(), "path": path, "role": role,
            "content_is_untrusted": True,
            "next_start_line": start + page.complete_lines if page.state == "ok"
                and page.complete_lines > 0 and start + page.complete_lines <= page.total_lines else None,
            "verified_absent": page.state == "not_found_at_revision",
        })
    except (ValueError, GitHubGatewayError, PostgreSQLRuntimeError) as exc:
        return error_output(str(exc))


@worker_lease_fence()
def docs_deliver(args: dict[str, Any], **context: Any) -> str:
    from .documentation_findings import finalize_documentation_assessment
    from .review_publication_application import prepare_postgres_documentation_publication

    try:
        source = gateway_source_session(args, context)
        result = _result(source)
        with postgres_runtime().transaction() as connection:
            identity = review_runs.get_run_scope(connection, ReviewRunId(source.run_id))
        if not result.frozen:
            review_run_snapshot(source=source, repository=identity.repository,
                pr_number=identity.pr_number, phase="rendering", expected_head_sha=result.head_sha)
            result = finalize_documentation_assessment(
                postgres_runtime(), run_id=source.run_id, assessment=args.get("assessment"),
                job_id=source.lease.job_id, lease_generation=source.lease.lease_generation,
            )
        configured = settings.ReviewAgentSettings.from_environment()
        prepared = prepare_postgres_documentation_publication(
            postgres_runtime(), run_id=source.run_id,
            max_comment_bytes=configured.publish_max_bytes,
            delivery_max_attempts=configured.publication_max_attempts,
            review_job_id=source.lease.job_id, review_lease_generation=source.lease.lease_generation,
        )
        return page_output({"stage": "queued_for_publication", "published": False,
            "run_id": source.run_id, "publication_id": prepared.publication_id,
            "outcome": result.outcome, "coverage_complete": result.coverage_complete})
    except ReviewRunTerminal as terminal:
        return output_json(run_terminal_payload(terminal.run_id))
    except (ValueError, GitHubGatewayError, PostgreSQLRuntimeError) as exc:
        return error_output(str(exc))
