"""Immutable documentation admission provenance and explicit-request promotion."""

from __future__ import annotations

from typing import Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ..domain.documentation_operating_policy import (
    DocumentationMode,
    ResolvedDocumentationMode,
)
from ..domain.review import ReviewPurpose, ReviewRunId
from . import jobs, review_runs, webhook_deliveries
from .. import failure_codes

DocumentationTrigger = Literal["manual", "automatic", "check_rerun"]


def record(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    delivery: webhook_deliveries.WebhookDelivery,
    trigger: DocumentationTrigger,
    base_ref: str | None,
    explicit_priority: int,
    policy: ResolvedDocumentationMode,
    policy_revision: str,
    admitted_team_id: int | None,
) -> None:
    scope = review_runs.get_run_scope(connection, run_id)
    if scope.run.purpose is not ReviewPurpose.DOCUMENTATION:
        raise ValueError("documentation admission requires documentation purpose")
    resolved = policy
    payload = delivery.normalized_payload or {}
    provenance = {
        "delivery_guid": delivery.delivery_guid,
        "event": delivery.event,
        "action": delivery.action,
        "sender_id": payload.get("sender_id"),
        "sender_login": payload.get("sender_login"),
        "comment_id": payload.get("comment_id"),
        "check_run_id": payload.get("check_run_id"),
    }
    row = connection.execute(
        """INSERT INTO review_agent.documentation_admissions
        (review_run_id, delivery_id, trigger_kind, trigger_json, policy_json)
        VALUES (%s, %s, %s, %s, %s) ON CONFLICT (review_run_id) DO NOTHING RETURNING review_run_id""",
        (
            run_id,
            delivery.id,
            trigger,
            Jsonb(provenance),
            Jsonb(
                {
                    "revision": policy_revision,
                    "team_id": admitted_team_id,
                    "configured_mode": resolved.configured_mode.value,
                    "effective_mode": resolved.effective_mode.value,
                    "source": resolved.source,
                    "deployment_enabled": resolved.deployment_enabled,
                    "base_ref": base_ref,
                }
            ),
        ),
    ).fetchone()
    if row is None and trigger != "automatic":
        connection.execute(
            """UPDATE review_agent.documentation_admissions
            SET promoted_delivery_id = %s, promotion_json = %s, promoted_at = statement_timestamp()
            WHERE review_run_id = %s AND trigger_kind = 'automatic' AND promoted_delivery_id IS NULL""",
            (delivery.id, Jsonb(provenance), run_id),
        )
    if trigger != "automatic":
        connection.execute(
            """UPDATE review_agent.review_jobs SET priority = GREATEST(priority, %s),
            available_at = LEAST(available_at, statement_timestamp())
            WHERE review_run_id = %s AND status = 'queued'""",
            (explicit_priority, run_id),
        )


def is_automatic(connection: psycopg.Connection[TupleRow], run_id: ReviewRunId) -> bool:
    row = connection.execute(
        "SELECT trigger_kind = 'automatic' AND promoted_delivery_id IS NULL FROM review_agent.documentation_admissions WHERE review_run_id = %s",
        (run_id,),
    ).fetchone()
    return row is not None and row[0] is True


def cancel_ineligible(
    connection: psycopg.Connection[TupleRow],
    *,
    effective_mode: DocumentationMode,
    repository_id: int | None = None,
) -> int:
    """Retire affected queued work; current authorization fences every later use."""
    if effective_mode is DocumentationMode.AUTOMATIC:
        return 0
    rows = connection.execute(
        """SELECT run.id, pull.repository_id, job.status,
            admission.trigger_kind = 'automatic' AND admission.promoted_delivery_id IS NULL AS automatic
        FROM review_agent.review_runs run JOIN review_agent.pull_requests pull ON pull.id = run.pull_request_id
        JOIN review_agent.review_jobs job ON job.review_run_id = run.id
        LEFT JOIN review_agent.documentation_admissions admission ON admission.review_run_id = run.id
        WHERE run.purpose = 'documentation' AND run.status = 'running'
          AND job.status IN ('queued', 'leased') AND (%s::bigint IS NULL OR pull.repository_id = %s)
        ORDER BY run.id""",
        (repository_id, repository_id),
    ).fetchall()
    count = 0
    for run_id, _owner_id, job_status, automatic in rows:
        if effective_mode is DocumentationMode.OFF or (
            automatic and job_status == "queued"
        ):
            scope = review_runs.get_run_scope(connection, ReviewRunId(run_id))
            connection.execute(
                "SELECT id FROM review_agent.pull_requests WHERE id = %s FOR NO KEY UPDATE",
                (scope.run.pull_request_id,),
            )
            current = review_runs.lock_run(connection, ReviewRunId(run_id))
            job = connection.execute(
                "SELECT status FROM review_agent.review_jobs WHERE review_run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            still_ineligible = effective_mode is DocumentationMode.OFF or (
                is_automatic(connection, current.id)
                and job is not None
                and job[0] == "queued"
            )
            if (
                current.status.value == "running"
                and job is not None
                and job[0] in {"queued", "leased"}
                and still_ineligible
            ):
                review_runs.fail_run(
                    connection,
                    current.id,
                    failure_code=failure_codes.DOCUMENTATION_DISABLED,
                )
                jobs.reconcile_run_jobs(
                    connection,
                    run_ids=(current.id,),
                    status=review_runs.get_run(connection, current.id).status,
                )
                count += 1
    pending = connection.execute(
        """SELECT delivery.id, repository.id, delivery.automatic_documentation
        FROM review_agent.github_webhook_deliveries delivery
        JOIN review_agent.repositories repository ON repository.provider = 'github'
            AND repository.provider_repository_id = delivery.provider_repository_id
        WHERE delivery.review_purpose = 'documentation' AND delivery.command_category = 'review'
          AND delivery.status = 'received' AND (%s::bigint IS NULL OR repository.id = %s)
        ORDER BY delivery.id""",
        (repository_id, repository_id),
    ).fetchall()
    for delivery_id, _owner_id, automatic in pending:
        if effective_mode is DocumentationMode.OFF or (automatic):
            connection.execute(
                """UPDATE review_agent.github_webhook_deliveries
                SET status = 'ignored', failure_code = 'documentation_disabled', failure_actor = 'documentation-policy',
                    completed_by = 'documentation-policy', processed_at = statement_timestamp()
                WHERE id = %s AND status = 'received'""",
                (delivery_id,),
            )
    return count


def cancel_for_pull(
    connection: psycopg.Connection[TupleRow],
    *,
    provider_repository_id: int,
    pr_number: int,
    automatic_only: bool,
) -> None:
    rows = connection.execute(
        """SELECT run.id, pull.id FROM review_agent.review_runs run
        JOIN review_agent.pull_requests pull ON pull.id = run.pull_request_id
        JOIN review_agent.repositories repository ON repository.id = pull.repository_id
        JOIN review_agent.documentation_admissions admission ON admission.review_run_id = run.id
        JOIN review_agent.review_jobs job ON job.review_run_id = run.id
        WHERE repository.provider_repository_id = %s AND pull.number = %s AND run.status = 'running'
          AND run.purpose = 'documentation' AND (%s = FALSE OR (admission.trigger_kind = 'automatic'
          AND admission.promoted_delivery_id IS NULL)) AND job.status IN ('queued', 'leased')
        ORDER BY run.id""",
        (provider_repository_id, pr_number, automatic_only),
    ).fetchall()
    for run_id, pull_id in rows:
        connection.execute(
            "SELECT id FROM review_agent.pull_requests WHERE id = %s FOR NO KEY UPDATE",
            (pull_id,),
        )
        current = review_runs.lock_run(connection, ReviewRunId(run_id))
        job = connection.execute(
            "SELECT status FROM review_agent.review_jobs WHERE review_run_id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if (
            current.status.value == "running"
            and (not automatic_only or is_automatic(connection, current.id))
            and job is not None
            and job[0] in {"queued", "leased"}
        ):
            review_runs.fail_run(
                connection,
                current.id,
                failure_code=failure_codes.DOCUMENTATION_INELIGIBLE,
            )
            jobs.reconcile_run_jobs(
                connection,
                run_ids=(current.id,),
                status=review_runs.get_run(connection, current.id).status,
            )
