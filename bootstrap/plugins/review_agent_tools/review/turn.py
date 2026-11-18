"""One model turn of a claimed review job: what the worker hands over and how it can fail."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.review import JsonObject, ReviewPurpose
from ..postgres import jobs
from ..worker_telemetry import TokenUsage


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
