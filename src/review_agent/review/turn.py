"""One model turn of a claimed review job: what the worker hands over and how it can fail."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.review import JsonObject, ReviewPurpose
from ..postgres import jobs
from ..postgres.telemetry import TokenUsage
from . import part_instructions


@dataclass(frozen=True, slots=True)
class ClaimedReview:
    job: jobs.ReviewJob
    repository: str
    pr_number: int
    resolved_config: JsonObject
    purpose: ReviewPurpose = ReviewPurpose.CODE
    # A review split into parts: this job's part (zero is the main job) and the
    # number of parts planned for the run (zero for a review that was not split).
    part: int = 0
    parts: int = 0


class ReviewTurnError(RuntimeError):
    """One review turn was not accepted or did not finish.

    ``code`` and ``detail`` record why the attempt ended for operators; the
    detail is code-owned or text the engine redacted. ``usage`` keeps the tokens a
    failed turn still reported.
    """

    retryable: bool
    code: str
    detail: str | None
    usage: TokenUsage | None

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        code: str = "hermes_request_failed",
        detail: str | None = None,
        usage: TokenUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.detail = detail
        self.usage = usage


def turn_messages(
    claimed: ClaimedReview,
    *,
    code_instructions: str,
    documentation_instructions: str | None,
) -> tuple[str, str]:
    """Return the system instructions and the request that start this turn."""
    instructions = code_instructions
    first_call = (
        "Call review_agent_begin first with repository "
        f"{claimed.repository!r}, pr_number {claimed.pr_number}, "
        f"and existing_run_id {int(claimed.job.review_run_id)}. "
    )
    if claimed.purpose is ReviewPurpose.DOCUMENTATION:
        if documentation_instructions is None:
            raise ReviewTurnError(
                "Documentation review skill is not installed",
                retryable=False, code="worker_misconfigured",
            )
        instructions = documentation_instructions
        first_call = f"Call review_agent_docs_begin first with run_id {int(claimed.job.review_run_id)}. "
    request_text = (
        "Continue the assigned durable pull-request review. "
        f"{first_call}"
        "Follow the review skill and finish through deterministic "
        "delivery."
    )
    if claimed.part:
        instructions += part_instructions.PART_INSTRUCTIONS
        request_text = part_instructions.part_request(
            first_call, part=claimed.part, parts=claimed.parts
        )
    elif claimed.parts:
        instructions += part_instructions.FINAL_INSTRUCTIONS
        request_text = part_instructions.final_request(
            first_call, parts=claimed.parts
        )
    return instructions, request_text
