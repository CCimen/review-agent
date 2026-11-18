"""One model turn of a claimed review job: what the worker hands over and how it can fail.

A turn is one inference execution for one generation of a leased job. The
worker sends one request and waits. Three things then happen independently, and
none of them is inferred from another:

1. The worker stops waiting. Its request returns, fails or times out;
   ``WorkerPolicy.request_timeout`` bounds each blocking read, not the turn.
   None of these says whether inference stopped or whether the review finished,
   and a normal return may carry no usage.
2. The engine's handler ends. When it returns a response or rejects the request
   with an HTTP error, the engine side drops the execution's context window and
   then sets ``model_executions.finished_at``, whether or not the lease is still
   live. A cancelled handler, an unexpected handler error and a lost engine
   process skip both, because none of them proves that inference stopped. If
   the write is rolled back the reservation stays open with the window already
   gone. An open reservation keeps its job unclaimable and keeps occupying its
   connection, also after the lease expires or the run ends. It ends when the
   handler does; one that was abandoned is closed when a platform owner
   reconciles the connection against a restarted runtime.
3. PostgreSQL holds the job's state, and it is the only authority for it. The
   model's tool calls change it (a publication handoff, a stored part report, a
   split that requeues the main job), and so can others while the turn runs,
   such as a newer head superseding the review. When its request has ended, a
   worker that already knows it lost the lease does nothing. Otherwise it reads
   the job, leaves one that is no longer leased alone, and records a failure
   only through a write fenced by its lease owner and generation.

Identity: the job id and lease generation (``WorkerLeaseSession``) travel as the
session id. Each model tool checks that generation and its review run against
PostgreSQL on entry (``tool_runtime.worker_lease_fence``). A lost lease refuses
later tool calls; it does not interrupt a call that already passed the check.

Admission: before inference the engine side resolves the context window, then
in one transaction checks the live lease, the connection, provider, model and
effort the subject was assigned, the account revision and the account identity
observed now, reserves the execution, and compares the behaviour receipt frozen
in ``resolved_config`` with the installed one
(``contract.require_matching_execution_contract``: only the queued provider,
model and effort may differ from the installed default). A mismatch refuses the
turn, which ends the attempt for a worker that still holds the claim. A paused
connection or an account waiting for quota returns the job to the queue without
spending an attempt. The window is registered for the job generation after the
commit, and every tool call of the execution takes its result budgets from it
(``capacity.for_window``).

The worker gets optional, bounded usage back, or ``ReviewTurnError`` for the
failures the client expects.
"""

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
