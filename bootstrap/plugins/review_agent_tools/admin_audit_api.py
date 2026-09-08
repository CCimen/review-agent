"""Restricted audit queries and bounded JSON, CSV, JSONL and OTLP exports."""

import csv
import io
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import AwareDatetime, TypeAdapter

from . import admin_application
from .admin_auth import AdminAuth
from .postgres import audit
from .postgres.runtime import PostgreSQLRuntime
from .postgres.team_access import AccessRequest


class AuditFormat(StrEnum):
    JSON = "json"
    CSV = "csv"
    JSONL = "jsonl"
    OTLP = "otlp"


_PAGE = TypeAdapter(audit.AuditPage)
_EVENT = TypeAdapter(audit.AuditEvent)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _filters(
    before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    actor_id: UUID | None = None,
    action: audit.AuditAction | None = None,
    outcome: audit.AuditOutcome | None = None,
    search: Annotated[str, Query(max_length=200)] = "",
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> audit.AuditFilters:
    if since is not None and until is not None and since >= until:
        raise HTTPException(
            422, "The start must be before the end of the audit period."
        )
    return audit.AuditFilters(
        before_id, actor_id, action, outcome, search.strip(), since, until
    )


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    # Quoting CSV fields does not prevent spreadsheet formula interpretation.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + text
    return text


def _csv(page: audit.AuditPage) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "id",
            "recorded_at",
            "action",
            "outcome",
            "actor_id",
            "actor_email",
            "actor_role",
            "team_id",
            "subject",
            "reason",
            "details",
            "owner_only",
            "operation_id",
        )
    )
    for event in page.items:
        writer.writerow(
            _csv_cell(value)
            for value in (
                event.id,
                event.recorded_at.isoformat(),
                event.action.value,
                event.outcome.value,
                event.actor_id,
                event.actor_email,
                event.actor_role,
                event.team_id,
                event.subject,
                event.reason,
                json.dumps(event.details, ensure_ascii=False),
                event.owner_only,
                event.operation_id,
            )
        )
    return output.getvalue()


def _attribute(key: str, value: str | int | bool) -> dict[str, object]:
    encoded = (
        {"boolValue": value}
        if isinstance(value, bool)
        else {"intValue": str(value)}
        if isinstance(value, int)
        else {"stringValue": value}
    )
    return {"key": key, "value": encoded}


def _unix_nanos(value: datetime) -> str:
    elapsed = value - _EPOCH
    return str(
        (elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000
        + elapsed.microseconds * 1000
    )


def _otel_event(event: audit.AuditEvent, observed_at: str) -> dict[str, object]:
    values: dict[str, str | int | bool | None] = {
        "review_agent.audit.event_id": event.id,
        "review_agent.audit.action": event.action.value,
        "review_agent.audit.outcome": event.outcome.value,
        "review_agent.audit.actor_id": str(event.actor_id) if event.actor_id else None,
        "review_agent.audit.actor_email": event.actor_email,
        "review_agent.audit.actor_role": event.actor_role,
        "review_agent.audit.team_id": event.team_id,
        "review_agent.audit.subject": event.subject,
        "review_agent.audit.owner_only": event.owner_only,
        "review_agent.audit.operation_id": str(event.operation_id)
        if event.operation_id
        else None,
        **{
            f"review_agent.audit.details.{key}": value
            for key, value in event.details.items()
        },
    }
    return {
        "timeUnixNano": _unix_nanos(event.recorded_at),
        "observedTimeUnixNano": observed_at,
        "eventName": f"review_agent.audit.{event.action.value}",
        "severityNumber": 17 if event.outcome is audit.AuditOutcome.FAILED else 9,
        "severityText": "ERROR"
        if event.outcome is audit.AuditOutcome.FAILED
        else "INFO",
        "body": {"stringValue": event.reason},
        "attributes": [
            _attribute(key, value) for key, value in values.items() if value is not None
        ],
    }


def _otlp(page: audit.AuditPage) -> str:
    observed_at = _unix_nanos(datetime.now(timezone.utc))
    return json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [_attribute("service.name", "review-agent-admin")]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "review_agent.audit"},
                            "logRecords": [
                                _otel_event(event, observed_at) for event in page.items
                            ],
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    router = APIRouter(prefix="/api/audit", tags=["audit"])

    def events(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        filters: Annotated[audit.AuditFilters, Depends(_filters)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> audit.AuditPage:
        return admin_application.audit_events(
            runtime, access=access, limit=limit, filters=filters
        )

    def export(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        filters: Annotated[audit.AuditFilters, Depends(_filters)],
        format: AuditFormat = AuditFormat.JSON,
        limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
    ) -> Response:
        page = admin_application.audit_events(
            runtime, access=access, limit=limit, filters=filters
        )
        if format is AuditFormat.CSV:
            content, media_type, extension = _csv(page), "text/csv", "csv"
        elif format is AuditFormat.JSONL:
            content = b"".join(_EVENT.dump_json(event) + b"\n" for event in page.items)
            media_type, extension = "application/x-ndjson", "jsonl"
        elif format is AuditFormat.OTLP:
            content, media_type, extension = (
                _otlp(page),
                "application/json",
                "otlp.json",
            )
        else:
            content, media_type, extension = (
                _PAGE.dump_json(page),
                "application/json",
                "json",
            )
        ids = f"{page.items[0].id}-{page.items[-1].id}" if page.items else "empty"
        headers = {
            "Content-Disposition": f'attachment; filename="review-agent-audit-{ids}.{extension}"',
            "X-Audit-Count": str(len(page.items)),
        }
        if page.next_before_id is not None:
            headers["X-Audit-Next-Before-ID"] = str(page.next_before_id)
        return Response(content, media_type=media_type, headers=headers)

    router.add_api_route(
        "",
        events,
        methods=["GET"],
        summary="Search audit events visible to the current owner or admin",
    )
    router.add_api_route(
        "/export",
        export,
        methods=["GET"],
        response_class=Response,
        summary="Export one bounded page of matching audit events",
        description="Uses the same filters and permissions as the audit list. since is inclusive; until is exclusive. Follow X-Audit-Next-Before-ID as before_id for older events. CSV prefixes formula-like cells with an apostrophe; JSON and JSONL preserve exact values. OTLP exports are OpenTelemetry JSON log requests; no external collector is contacted.",
        responses={
            200: {
                "content": {
                    media: {"schema": {"type": "string", "format": "binary"}}
                    for media in (
                        "application/json",
                        "text/csv",
                        "application/x-ndjson",
                    )
                },
                "headers": {
                    "X-Audit-Count": {
                        "schema": {"type": "integer"},
                        "description": "Number of events in this file.",
                    },
                    "X-Audit-Next-Before-ID": {
                        "schema": {"type": "integer"},
                        "description": "Present when more matching events remain; send as before_id for the next page.",
                    },
                },
            }
        },
    )
    return router
