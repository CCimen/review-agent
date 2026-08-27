"""Deterministic review validation and publication handoff tool."""

from __future__ import annotations

from typing import Any

from . import (
    failure_codes,
    review_publication_application,
    review_publication_planner,
    review_run_application,
    settings,
)
from .domain.review import ReviewRunId
from .postgres.coverage import CoverageSummary
from .postgres.runtime import PostgreSQLRuntimeError
from .review_tool_runtime import (
    ReviewRunTerminal,
    ToolInputError,
    GatewaySourceSession,
    error_output,
    gateway_source_session,
    mark_run_failed,
    output_json,
    postgres_runtime,
    pull_request_identity,
    pull_head_sha,
    review_run_snapshot,
    run_subject,
    run_terminal_payload,
    worker_lease_fence,
)


def _recoverable_diff_gap(coverage: CoverageSummary) -> int:
    """Count registered paths Hermes can still diff-review before delivery."""
    if (
        not coverage.registration_complete
        or coverage.changed_files_reported != coverage.changed_files_registered
    ):
        return 0
    return coverage.unseen_paths


@worker_lease_fence()
def review_deliver(args: dict[str, Any], **context: Any) -> str:
    repository = ""
    number = 0
    run_id = 0
    head_sha = ""
    source: GatewaySourceSession | None = None
    try:
        source = gateway_source_session(args, context)
        repository, number, initial_pull = pull_request_identity(source)
        head_sha = pull_head_sha(initial_pull)
        run_id = source.run_id

        persisted = review_run_application.load_live_run_state(
            postgres_runtime(),
            run_subject(
                repository=repository,
                pr_number=number,
                run_id=run_id,
            ),
        )
        pull = review_run_snapshot(
            source=source,
            repository=repository,
            pr_number=number,
            # A frozen publication plan is already in the publishing phase and
            # can be replayed idempotently after a worker reclaim.
            phase=(
                "publishing" if persisted.phase == "publishing" else "rendering"
            ),
            expected_head_sha=head_sha,
        )
        if pull.get("state") != "open":
            raise ToolInputError("the pull request is no longer open")
        # Reaching rendering records that deterministic coverage recovery was
        # already requested once; a later delivery must remain publishable.
        if persisted.phase not in {"rendering", "publishing"}:
            coverage = review_run_application.summarize_postgres_coverage(
                postgres_runtime(), ReviewRunId(run_id)
            )
            recoverable_paths = _recoverable_diff_gap(coverage)
            if recoverable_paths:
                return output_json(
                    {
                        "stage": "validation_failed",
                        "published": False,
                        "retryable": True,
                        "run_id": run_id,
                        "error": (
                            f"{recoverable_paths} registered changed path(s) "
                            "have not been diff-reviewed"
                        ),
                        "changed_paths_registered": (
                            coverage.changed_files_registered
                        ),
                        "changed_paths_with_complete_diff": (
                            coverage.changed_paths_with_complete_diff
                        ),
                        "changed_paths_unseen": coverage.unseen_paths,
                        "next_action": (
                            "Call review_agent_pr_files and page the changed paths. "
                            "For every item whose diff_state is unseen, call "
                            "review_agent_pr_diff and follow any continuation. Then "
                            "call review_agent_deliver again with this same run_id."
                        ),
                    }
                )
        configured = settings.ReviewAgentSettings.from_environment()
        prepared = review_publication_application.prepare_postgres_publication(
            postgres_runtime(),
            run_id=run_id,
            previous_verdicts=args.get("previous_verdicts"),
            feedback_enabled=configured.feedback_enabled,
            max_comment_bytes=configured.publish_max_bytes,
            delivery_max_attempts=configured.publication_max_attempts,
            review_job_id=source.lease.job_id,
            review_lease_generation=source.lease.lease_generation,
        )
        return output_json(
            {
                "stage": "queued_for_publication",
                "published": False,
                "run_id": run_id,
                "publication_id": prepared.publication_id,
                "delivery_status": "generated",
                "findings_count": prepared.findings_count,
                "suggestions_count": prepared.suggestions_count,
                "resolved_count": prepared.resolved_count,
                "ignored_previous_verdicts": list(
                    prepared.ignored_previous_verdicts
                ),
            }
        )
    except ReviewRunTerminal as terminal:
        return output_json(run_terminal_payload(terminal.run_id))
    except review_publication_planner.PublicationPlanningError as exc:
        if repository and number and run_id:
            if source is None:
                return error_output("review source session is unavailable")
            try:
                review_run_snapshot(
                    source=source,
                    repository=repository,
                    pr_number=number,
                    phase="rendering",
                    expected_head_sha=head_sha,
                )
            except ReviewRunTerminal as terminal:
                return output_json(run_terminal_payload(terminal.run_id))
            except (
                ToolInputError,
                review_run_application.ReviewRunError,
                PostgreSQLRuntimeError,
            ) as phase_error:
                return error_output(str(phase_error))
            return output_json(
                {
                    "stage": "validation_failed",
                    "published": False,
                    "retryable": True,
                    "run_id": run_id,
                    "error": str(exc),
                    "next_action": (
                        "Align previous_verdicts with the recorded findings. Re-record "
                        "any omitted still-current finding or use not_checked when it "
                        "was not rechecked, then call review_agent_deliver again with "
                        "this same run_id."
                    ),
                }
            )
        return error_output(str(exc))
    except (
        ToolInputError,
        review_run_application.ReviewRunError,
        PostgreSQLRuntimeError,
    ) as exc:
        if repository and number and run_id:
            mark_run_failed(
                repository=repository,
                pr_number=number,
                run_id=run_id,
                failure_code=failure_codes.REVIEW_DELIVER_ERROR,
            )
        return error_output(str(exc))
    except Exception:
        if repository and number and run_id:
            mark_run_failed(
                repository=repository,
                pr_number=number,
                run_id=run_id,
                failure_code=failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE,
            )
        return error_output("unexpected review-deliver failure")
