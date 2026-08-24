"""Durable dry-run state for private reviewer-coach proposals."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

try:
    from .domain.coaching import (
        CoachCandidateInput as CoachCandidateInput,
        CoachingDomainError,
        CoachRunDecision as CoachRunDecision,
        CoachRunInput as CoachRunInput,
        resolve_coach_repository,
        resolve_coach_run,
    )
    from .memory_validation import (
        ReviewMemoryError,
        clean_text,
        isoformat,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from domain.coaching import (
        CoachCandidateInput as CoachCandidateInput,
        CoachingDomainError,
        CoachRunDecision as CoachRunDecision,
        CoachRunInput as CoachRunInput,
        resolve_coach_repository,
        resolve_coach_run,
    )
    from memory_validation import (
        ReviewMemoryError,
        clean_text,
        isoformat,
    )


@dataclass(frozen=True)
class CoachRunRow:
    id: int
    repository: str
    source_event_set_id: str
    source_snapshot_id: str
    proposal_set_id: str
    decision: CoachRunDecision
    events_considered: int
    candidates_count: int
    artifact_dir: str
    recorded_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "id": self.id,
            "repository": self.repository,
            "source_event_set_id": self.source_event_set_id,
            "source_snapshot_id": self.source_snapshot_id,
            "proposal_set_id": self.proposal_set_id,
            "decision": self.decision,
            "events_considered": self.events_considered,
            "candidates_count": self.candidates_count,
            "artifact_dir": self.artifact_dir,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class CoachCandidateRow:
    repository: str
    candidate_key: str
    proposal_set_id: str
    source_event_set_id: str
    target_owner: str
    suggested_route: str
    event_type: str
    independent_episode_count: int
    evidence_event_ids: tuple[str, ...]
    evidence_events_total: int
    first_seen_at: str
    last_seen_at: str
    seen_count: int

    def to_json_obj(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "candidate_key": self.candidate_key,
            "proposal_set_id": self.proposal_set_id,
            "source_event_set_id": self.source_event_set_id,
            "target_owner": self.target_owner,
            "suggested_route": self.suggested_route,
            "event_type": self.event_type,
            "independent_episode_count": self.independent_episode_count,
            "evidence_event_ids": list(self.evidence_event_ids),
            "evidence_events_total": self.evidence_events_total,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "seen_count": self.seen_count,
        }


def record_coach_run(
    connection: sqlite3.Connection,
    item: CoachRunInput,
    *,
    now: datetime | None = None,
) -> CoachRunRow:
    try:
        definition = resolve_coach_run(item)
    except CoachingDomainError as exc:
        raise ReviewMemoryError(str(exc)) from exc
    recorded_at = isoformat(now)
    candidates = definition.candidates

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO coach_runs (
                repository, source_event_set_id, source_snapshot_id, proposal_set_id,
                decision, events_considered, candidates_count, artifact_dir, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                definition.repository or "",
                definition.source_event_set_id,
                definition.source_snapshot_id or "",
                definition.proposal_set_id,
                definition.decision,
                definition.events_considered,
                len(candidates),
                definition.artifact_dir or "",
                recorded_at,
            ),
        )
        for candidate in candidates:
            evidence_json = _evidence_json(candidate.evidence_event_ids)
            connection.execute(
                """
                INSERT INTO coach_candidates (
                    repository, candidate_key, proposal_set_id, source_event_set_id,
                    target_owner, suggested_route, event_type,
                    independent_episode_count, evidence_event_ids_json,
                    evidence_events_total, first_seen_at, last_seen_at, seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(repository, candidate_key) DO UPDATE SET
                    proposal_set_id = excluded.proposal_set_id,
                    source_event_set_id = excluded.source_event_set_id,
                    target_owner = excluded.target_owner,
                    suggested_route = excluded.suggested_route,
                    event_type = excluded.event_type,
                    independent_episode_count = excluded.independent_episode_count,
                    evidence_event_ids_json = excluded.evidence_event_ids_json,
                    evidence_events_total = excluded.evidence_events_total,
                    last_seen_at = excluded.last_seen_at,
                    seen_count = coach_candidates.seen_count + 1
                """,
                (
                    definition.repository or "",
                    candidate.candidate_key,
                    definition.proposal_set_id,
                    definition.source_event_set_id,
                    candidate.target_owner,
                    candidate.suggested_route,
                    candidate.event_type,
                    candidate.independent_episode_count,
                    evidence_json,
                    candidate.evidence_events_total,
                    recorded_at,
                    recorded_at,
                ),
            )

    if cursor.lastrowid is None:
        raise ReviewMemoryError("failed to record coach run")
    return CoachRunRow(
        id=cursor.lastrowid,
        repository=definition.repository or "",
        source_event_set_id=definition.source_event_set_id,
        source_snapshot_id=definition.source_snapshot_id or "",
        proposal_set_id=definition.proposal_set_id,
        decision=definition.decision,
        events_considered=definition.events_considered,
        candidates_count=len(candidates),
        artifact_dir=definition.artifact_dir or "",
        recorded_at=recorded_at,
    )


def list_coach_runs(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    limit: int = 50,
) -> tuple[CoachRunRow, ...]:
    limit = max(1, min(int(limit), 500))
    params: list[object] = []
    where = ""
    if repository:
        where = "WHERE repository = ?"
        try:
            params.append(resolve_coach_repository(repository) or "")
        except CoachingDomainError as exc:
            raise ReviewMemoryError(str(exc)) from exc
    params.append(limit)
    rows = connection.execute(
        f"SELECT * FROM coach_runs {where} ORDER BY recorded_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    return tuple(_coach_run_row(row) for row in rows)


def list_coach_candidates(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    limit: int = 50,
) -> tuple[CoachCandidateRow, ...]:
    limit = max(1, min(int(limit), 500))
    params: list[object] = []
    where = ""
    if repository:
        where = "WHERE repository = ?"
        try:
            params.append(resolve_coach_repository(repository) or "")
        except CoachingDomainError as exc:
            raise ReviewMemoryError(str(exc)) from exc
    params.append(limit)
    rows = connection.execute(
        f"""
        SELECT * FROM coach_candidates {where}
        ORDER BY last_seen_at DESC, candidate_key ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return tuple(_coach_candidate_row(row) for row in rows)


def _coach_run_row(row: sqlite3.Row) -> CoachRunRow:
    return CoachRunRow(
        id=int(row["id"]),
        repository=str(row["repository"]),
        source_event_set_id=str(row["source_event_set_id"]),
        source_snapshot_id=str(row["source_snapshot_id"]),
        proposal_set_id=str(row["proposal_set_id"]),
        decision=_decision(str(row["decision"])),
        events_considered=int(row["events_considered"]),
        candidates_count=int(row["candidates_count"]),
        artifact_dir=str(row["artifact_dir"]),
        recorded_at=str(row["recorded_at"]),
    )


def _coach_candidate_row(row: sqlite3.Row) -> CoachCandidateRow:
    return CoachCandidateRow(
        repository=str(row["repository"]),
        candidate_key=str(row["candidate_key"]),
        proposal_set_id=str(row["proposal_set_id"]),
        source_event_set_id=str(row["source_event_set_id"]),
        target_owner=str(row["target_owner"]),
        suggested_route=str(row["suggested_route"]),
        event_type=str(row["event_type"]),
        independent_episode_count=int(row["independent_episode_count"]),
        evidence_event_ids=_parse_evidence_ids(str(row["evidence_event_ids_json"])),
        evidence_events_total=int(row["evidence_events_total"]),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
        seen_count=int(row["seen_count"]),
    )


def _decision(value: str) -> CoachRunDecision:
    if value == "propose" or value == "no_change":
        return value
    raise ReviewMemoryError("coach run decision must be propose or no_change")


def _evidence_json(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _parse_evidence_ids(raw: str) -> tuple[str, ...]:
    try:
        value: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewMemoryError("coach candidate evidence_event_ids_json is invalid") from exc
    if not isinstance(value, list):
        raise ReviewMemoryError("coach candidate evidence_event_ids_json must be a list")
    raw_items = cast(Sequence[object], value)
    items: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, str) or not item.strip():
            raise ReviewMemoryError(
                f"coach candidate evidence_event_ids_json[{index}] must be a string"
            )
        items.append(clean_text(item, field="evidence_event_id", maximum=120))
    return tuple(sorted(set(items)))
