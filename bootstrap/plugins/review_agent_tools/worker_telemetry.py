"""Best-effort process presence and bounded, allowlisted operational events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import threading
from typing import Literal, cast
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow

from .postgres.admin_operations import WorkerKind
from .postgres.runtime import PostgreSQLRuntime, PostgreSQLRuntimeError

logger = logging.getLogger(__name__)
_ERRORS = (psycopg.Error, PostgreSQLRuntimeError)
Event = Literal[
    "started",
    "draining",
    "stopped",
    "review_started",
    "review_returned",
    "usage_unavailable",
]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def parse_usage(body: bytes) -> TokenUsage | None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_usage = cast(dict[str, object], payload).get("usage")
    if not isinstance(raw_usage, dict):
        return None
    usage = cast(dict[str, object], raw_usage)
    values: list[int] = []
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is not int or not 0 <= value <= 9223372036854775807:
            return None
        values.append(value)
    prompt, completion, total = values
    if total <= 0 or prompt + completion != total:
        return None
    return TokenUsage(prompt, completion, total)


def record_usage(
    runtime: PostgreSQLRuntime, *, job_id: int, generation: int, usage: TokenUsage
) -> None:
    try:
        with runtime.transaction() as connection:
            connection.execute("SET LOCAL statement_timeout = '2s'")
            connection.execute(
                """
                INSERT INTO review_agent.review_attempt_usage
                    (job_id, lease_generation, prompt_tokens, completion_tokens, total_tokens)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id, lease_generation) DO NOTHING
            """,
                (
                    job_id,
                    generation,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                ),
            )
    except _ERRORS:
        logger.warning("Review token usage could not be recorded")


class WorkerTelemetry:
    """Observe a process without owning its job state or shutdown decision."""

    def __init__(
        self,
        runtime: PostgreSQLRuntime,
        *,
        kind: WorkerKind,
        lease_owner: str,
        capacity: int,
        stop_event: threading.Event,
    ) -> None:
        self._runtime = runtime
        self.id = uuid4()
        self._started_at = datetime.now(timezone.utc)
        self._kind = kind
        self._owner = lease_owner
        self._capacity = capacity
        self._worker_stop = stop_event
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat, name="worker-presence", daemon=True
        )

    def __enter__(self) -> WorkerTelemetry:
        self.refresh("running")
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._done.set()
        self._thread.join()
        self.refresh("stopped")

    def _heartbeat(self) -> None:
        while not self._done.wait(30):
            self.refresh("draining" if self._worker_stop.is_set() else "running")

    def refresh(self, state: Literal["running", "draining", "stopped"]) -> None:
        try:
            with self._runtime.transaction() as connection:
                connection.execute("SET LOCAL statement_timeout = '2s'")
                previous = connection.execute(
                    "SELECT state FROM review_agent.worker_instances WHERE id = %s FOR UPDATE",
                    (self.id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO review_agent.worker_instances (id, kind, lease_owner, capacity, state, started_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET last_seen_at = statement_timestamp(), state = EXCLUDED.state
                """,
                    (
                        self.id,
                        self._kind,
                        self._owner,
                        self._capacity,
                        state,
                        self._started_at,
                    ),
                )
                if previous is None or previous[0] != state:
                    event = "started" if state == "running" else state
                    self._write_event(connection, event)
                # Expire stale process diagnostics in bounded batches. Token totals
                # belong to durable review attempts and survive this cleanup.
                connection.execute("""
                    DELETE FROM review_agent.worker_instances WHERE id IN (
                        SELECT id FROM review_agent.worker_instances
                        WHERE last_seen_at < statement_timestamp() - interval '7 days'
                        ORDER BY last_seen_at LIMIT 100
                    )
                """)
        except _ERRORS:
            logger.warning("Worker presence could not be recorded")

    def event(self, event: Event, *, run_id: int, job_id: int) -> None:
        try:
            with self._runtime.transaction() as connection:
                connection.execute("SET LOCAL statement_timeout = '2s'")
                self._write_event(connection, event, run_id=run_id, job_id=job_id)
        except _ERRORS:
            logger.warning("Worker event could not be recorded")

    def _write_event(
        self,
        connection: psycopg.Connection[TupleRow],
        event: Event,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO review_agent.worker_events (worker_id, event, review_run_id, job_id)
            SELECT id, %s, %s, %s FROM review_agent.worker_instances WHERE id = %s
            """,
            (event, run_id, job_id, self.id),
        )
        # Fixed event codes keep raw logs and provider output out of storage.
        # Retain at most 1,000 events, including the final shutdown event.
        connection.execute(
            """
            DELETE FROM review_agent.worker_events WHERE worker_id = %s AND id < (
                SELECT id FROM review_agent.worker_events WHERE worker_id = %s
                ORDER BY id DESC OFFSET 999 LIMIT 1
            )
            """,
            (self.id, self.id),
        )
