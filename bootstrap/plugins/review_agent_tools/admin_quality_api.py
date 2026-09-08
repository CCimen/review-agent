"""Authenticated admin transport for quality reports and human decisions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from . import operator_application
from .admin_auth import AdminAuth, User
from .domain.feedback import FeedbackTargetOwner, FeedbackTriageStatus
from .domain.finding import DecisionKind
from .postgres import admin_quality, quality_reporting, quality_triage, reporting
from .postgres.runtime import PostgreSQLRuntime


Repository = Annotated[
    str, Query(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
]
OptionalRepository = Annotated[
    str | None,
    Query(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200),
]
Fingerprint = Annotated[str, Path(pattern=r"^[0-9a-f]{8,64}$")]


class FindingDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    ]
    occurrence_id: Annotated[int, Field(ge=1)]
    decision: DecisionKind
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    adr_id: Annotated[str, Field(max_length=80)] = ""
    expires_days: Annotated[int | None, Field(ge=1, le=3650)] = None


class QualityTriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FeedbackTriageStatus
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    stable_key: Annotated[str, Field(max_length=160)] = ""
    target_owner: FeedbackTargetOwner | None = None
    evidence_reference: Annotated[str, Field(max_length=500)] = ""
    path: Annotated[str, Field(max_length=500)] = ""
    category: Annotated[str, Field(max_length=80)] = ""


def _actor(user: User) -> str:
    """Use the immutable authenticated account id for the audit actor."""
    return f"admin:{user.id}"


def _input_error(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    """Create quality routes over the existing operator application boundary."""
    router = APIRouter(dependencies=[Depends(auth.current_user)])

    def report(
        days: Annotated[int, Query(ge=1, le=90)] = 30,
        repository: OptionalRepository = None,
    ) -> quality_reporting.QualityReport:
        try:
            return operator_application.quality_report(
                runtime, repository=repository, days=days
            )
        except ValueError as exc:
            raise _input_error(exc) from exc

    def feedback(
        repository: OptionalRepository = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> admin_quality.QualityFeedbackPage:
        with runtime.transaction() as connection:
            return admin_quality.feedback_backlog(
                connection, repository=repository, limit=limit, offset=offset
            )

    def finding(
        fingerprint: Fingerprint,
        repository: Repository,
        occurrence_id: Annotated[
            int | None, Query(ge=1, le=9223372036854775807)
        ] = None,
        decisions_before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> admin_quality.AdminFindingDetail:
        try:
            detail = operator_application.show_finding(
                runtime,
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
                has_more_decisions=len(detail.decisions) > 100,
                next_decision_before_id=(
                    int(decisions[-1].id)
                    if len(detail.decisions) > 100 and decisions
                    else None
                ),
            )
        except reporting.FindingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise _input_error(exc) from exc

    def findings_for_review(
        run_id: Annotated[int, Path(ge=1)],
    ) -> admin_quality.ReviewFindingPage:
        with runtime.transaction() as connection:
            return admin_quality.review_findings(connection, run_id=run_id)

    def decide(
        fingerprint: Fingerprint,
        request: FindingDecisionRequest,
        actor: Annotated[User, Depends(auth.current_admin)],
    ) -> operator_application.OperatorDecisionResult:
        try:
            return operator_application.decide_finding(
                runtime,
                operator_application.OperatorDecisionRequest(
                    repository=request.repository,
                    fingerprint=fingerprint,
                    occurrence_id=request.occurrence_id,
                    decision=request.decision.value,
                    reason=request.reason,
                    actor=_actor(actor),
                    adr_id=request.adr_id,
                    expires_days=request.expires_days,
                ),
            )
        except reporting.FindingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise _input_error(exc) from exc

    def triage(
        feedback_id: Annotated[int, Path(ge=1)],
        request: QualityTriageRequest,
        actor: Annotated[User, Depends(auth.current_admin)],
    ) -> quality_triage.QualityFeedbackTriage:
        try:
            return operator_application.triage_review_feedback(
                runtime,
                feedback_id=feedback_id,
                status=request.status.value,
                stable_key=request.stable_key,
                target_owner=(
                    request.target_owner.value
                    if request.target_owner is not None
                    else ""
                ),
                evidence_reference=request.evidence_reference,
                path=request.path,
                category=request.category,
                actor=_actor(actor),
                reason=request.reason,
            )
        except quality_triage.QualityFeedbackNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise _input_error(exc) from exc

    router.add_api_route("/api/quality", report, methods=["GET"])
    router.add_api_route("/api/quality/feedback", feedback, methods=["GET"])
    router.add_api_route(
        "/api/findings/{fingerprint}", finding, methods=["GET"]
    )
    router.add_api_route(
        "/api/history/{run_id}/findings", findings_for_review, methods=["GET"]
    )
    router.add_api_route(
        "/api/findings/{fingerprint}/decisions", decide, methods=["POST"]
    )
    router.add_api_route(
        "/api/quality/feedback/{feedback_id}/triage", triage, methods=["POST"]
    )
    return router
