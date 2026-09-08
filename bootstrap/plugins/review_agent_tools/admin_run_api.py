"""Admin-only HTTP transport for fenced review-run controls."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, field_validator

from . import admin_application
from .admin_auth import AdminAuth
from .postgres.team_access import AccessRequest
from .domain.review import ReviewStatus
from .postgres import admin_run_actions, jobs, review_runs
from .postgres.runtime import PostgreSQLRuntime


class ActionAvailability(BaseModel):
    available: bool
    reason: str


class RunControlSnapshot(BaseModel):
    id: int
    status: ReviewStatus
    phase: str
    last_heartbeat_at: datetime


class JobControlSnapshot(BaseModel):
    id: int
    status: jobs.ReviewJobStatus
    lease_generation: int
    available_at: datetime
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None


class RunActionAvailability(BaseModel):
    release_retry: ActionAvailability
    cancel: ActionAvailability
    mark_stalled: ActionAvailability


class RunActionAudit(BaseModel):
    id: int
    action: admin_run_actions.RunAction
    actor: str
    reason: str
    previous_run_status: ReviewStatus
    previous_job_status: jobs.ReviewJobStatus
    expected_lease_generation: int
    expected_available_at: datetime
    stale_after_minutes: int | None
    recorded_at: datetime


class RunControls(BaseModel):
    run: RunControlSnapshot
    job: JobControlSnapshot | None
    actions: RunActionAvailability
    audit: list[RunActionAudit]


class RunActionRequest(BaseModel):
    action: admin_run_actions.RunAction
    expected_job_id: int = Field(ge=1)
    expected_lease_generation: int = Field(ge=0)
    expected_status: jobs.ReviewJobStatus
    expected_available_at: datetime
    reason: str = Field(min_length=1, max_length=500)
    stale_after_minutes: int | None = Field(default=None, ge=1, le=1440)

    @field_validator("expected_available_at")
    @classmethod
    def require_timestamp_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_available_at must include a timezone")
        return value

    @field_validator("reason")
    @classmethod
    def require_reason_text(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("reason is required")
        return normalized


RunId = Annotated[int, Path(ge=1, le=9223372036854775807)]


def _response(value: admin_run_actions.RunControls) -> RunControls:
    return RunControls(
        run=RunControlSnapshot(
            id=int(value.run.id),
            status=value.run.status,
            phase=value.run.phase.value,
            last_heartbeat_at=value.run.last_heartbeat_at,
        ),
        job=(
            JobControlSnapshot(
                id=value.job.id,
                status=value.job.status,
                lease_generation=value.job.lease_generation,
                available_at=value.job.available_at,
                lease_expires_at=value.job.lease_expires_at,
                last_heartbeat_at=value.job.last_heartbeat_at,
            )
            if value.job is not None
            else None
        ),
        actions=RunActionAvailability(
            release_retry=ActionAvailability(
                available=value.release_retry.available,
                reason=value.release_retry.reason,
            ),
            cancel=ActionAvailability(
                available=value.cancel.available,
                reason=value.cancel.reason,
            ),
            mark_stalled=ActionAvailability(
                available=value.mark_stalled.available,
                reason=value.mark_stalled.reason,
            ),
        ),
        audit=[
            RunActionAudit(
                id=item.id,
                action=item.action,
                actor=item.actor,
                reason=item.reason,
                previous_run_status=item.previous_run_status,
                previous_job_status=item.previous_job_status,
                expected_lease_generation=item.expected_lease_generation,
                expected_available_at=item.expected_available_at,
                stale_after_minutes=item.stale_after_minutes,
                recorded_at=item.recorded_at,
            )
            for item in value.audit
        ],
    )


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    router = APIRouter(
        dependencies=[Depends(auth.current_user)],
        tags=["run controls"],
    )

    def controls(run_id: RunId, access: Annotated[AccessRequest, Depends(auth.current_scope)]) -> RunControls:
        return _response(admin_application.run_controls(runtime, access=access, run_id=run_id))

    def apply_action(
        run_id: RunId,
        request: RunActionRequest,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> RunControls:
        try:
            result = admin_application.apply_run_action(runtime, access=access, run_id=run_id, request=admin_run_actions.ActionRequest(
                action=request.action,
                expected_job_id=request.expected_job_id,
                expected_lease_generation=request.expected_lease_generation,
                expected_status=request.expected_status,
                expected_available_at=request.expected_available_at,
                actor="",
                reason=request.reason,
                stale_after_minutes=request.stale_after_minutes,
            ))
        except review_runs.ReviewRunNotFound as exc:
            raise HTTPException(404, "Review run not found.") from exc
        except admin_run_actions.AdminRunActionStale as exc:
            raise HTTPException(409, str(exc)) from exc
        except (admin_run_actions.AdminRunActionError, jobs.ReviewJobError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return _response(result)

    router.add_api_route(
        "/api/history/{run_id}/controls", controls, methods=["GET"]
    )
    router.add_api_route(
        "/api/history/{run_id}/actions", apply_action, methods=["POST"]
    )
    return router
