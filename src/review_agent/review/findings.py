"""Historical finding context, persistence, and atomic-suggestion coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import TypeAlias, TypedDict

import psycopg

from ..domain import review_validation, suggestions as domain_suggestions
from ..domain.finding import (
    MAX_FINDINGS_PER_REVIEW,
    FindingDefinition,
    FindingDomainError,
    FindingId,
    FindingInput,
    FindingOccurrenceId,
    RepeatFinding,
    resolve_context_hash,
    resolve_finding,
    resolve_finding_path,
    require_unique_finding_identities,
)
from ..domain.review import ReviewRunId
from ..postgres import (
    decisions as postgres_decisions,
    findings as postgres_findings,
    reporting as postgres_reporting,
    suggestions as postgres_suggestions,
)
from ..postgres.runtime import PostgreSQLRuntime, PostgreSQLRuntimeError


_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HeadFileLoader = Callable[[str, int, int], str | None]
RecordedFinding = dict[str, object]
PostgresFindingBatch: TypeAlias = postgres_findings.FindingBatch


class ReviewFindingError(ValueError):
    """A finding does not belong to the changed files of its review snapshot."""


class FindingHistoryContext(TypedDict):
    repository: str
    paths: list[str]
    policy: str
    historical_suppressions: list[dict[str, object]]
    recent_findings: list[dict[str, object]]
    repeat_review_findings: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class FindingContextQuery:
    repository: str
    paths: tuple[str, ...]
    pr_number: int | None = None


@dataclass(frozen=True, slots=True)
class ChangedFileContext:
    path: str
    context_hash: str = ""
    context_hash_source: str = ""
    patch: str | None = None


@dataclass(frozen=True, slots=True)
class FindingRecordResult:
    batch: PostgresFindingBatch
    suggestions_recorded: int
    suggestion_statuses: tuple[domain_suggestions.SuggestionStatus, ...]


@dataclass(frozen=True, slots=True)
class LiveRecordedFinding:
    finding_id: int
    occurrence_id: int
    fingerprint: str
    local_reference: str
    context_hash: str
    suppressed: bool
    decision: str | None
    suggestion_status: domain_suggestions.SuggestionStatus


@dataclass(frozen=True, slots=True)
class LiveFindingRecordResult:
    items: tuple[LiveRecordedFinding, ...]
    suggestions_recorded: int


def _postgres_context_hash(changed_file: ChangedFileContext, head_sha: str) -> str:
    candidate = changed_file.context_hash.strip().lower()
    if changed_file.context_hash_source == "blob" and _SHA_RE.fullmatch(candidate):
        return resolve_context_hash(candidate)
    return head_sha


def resolve_finding_batch(
    *,
    head_sha: str,
    findings: Sequence[FindingInput],
    changed_files: Sequence[ChangedFileContext],
) -> tuple[FindingDefinition, ...]:
    """Apply the recording admission checks to one batch without persisting it."""
    if len(findings) > MAX_FINDINGS_PER_REVIEW:
        raise FindingDomainError(
            f"findings exceeds operational safety limit of {MAX_FINDINGS_PER_REVIEW}"
        )
    resolved_head = resolve_context_hash(head_sha)
    changed_by_path = {
        resolve_finding_path(item.path): item for item in changed_files
    }
    definitions: list[FindingDefinition] = []
    for item in findings:
        path = resolve_finding_path(item.path)
        changed_file = changed_by_path.get(path)
        if changed_file is None:
            raise ReviewFindingError(
                "every recorded finding must point to a changed pull-request file"
            )
        definitions.append(
            resolve_finding(
                replace(item, path=path),
                context_hash=_postgres_context_hash(changed_file, resolved_head),
            )
        )
    require_unique_finding_identities(definitions)
    return tuple(definitions)


def record_run_findings(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    head_sha: str,
    findings: Sequence[FindingInput],
    changed_files: Sequence[ChangedFileContext],
) -> PostgresFindingBatch:
    """Validate and atomically persist one pre-reviewed PostgreSQL finding batch."""
    definitions = resolve_finding_batch(
        head_sha=head_sha, findings=findings, changed_files=changed_files
    )
    with runtime.transaction() as connection:
        return postgres_findings.record_findings(
            connection,
            run_id=run_id,
            expected_head_sha=resolve_context_hash(head_sha),
            definitions=definitions,
        )


def _postgres_suggestion_rank(
    finding: FindingInput, index: int
) -> tuple[int, int, str, int]:
    return (
        int(review_validation.SEVERITY_PRIORITY.get(finding.severity.title(), 99)),
        -finding.publication_score,
        finding.rule_id,
        index,
    )


def _record_postgres_optional_suggestions(
    runtime: PostgreSQLRuntime,
    *,
    batch: PostgresFindingBatch,
    head_sha: str,
    findings: Sequence[FindingInput],
    changed_files: Sequence[ChangedFileContext],
    suggestions: Sequence[object | None],
    head_file_loader: HeadFileLoader | None,
) -> tuple[int, tuple[domain_suggestions.SuggestionStatus, ...]]:
    requested = [index for index, item in enumerate(suggestions) if item is not None]
    statuses: list[domain_suggestions.SuggestionStatus] = [
        "not_requested"
    ] * len(findings)
    context: postgres_suggestions.SuggestionContext | None = None
    decisions: Mapping[FindingId, postgres_decisions.SuppressionDecision] = {}
    if requested:
        try:
            with runtime.transaction() as connection:
                loaded_context = postgres_suggestions.load_context(
                    connection, batch
                )
                loaded_decisions = postgres_decisions.latest_suppression_decisions(
                    connection,
                    finding_ids=tuple(item.finding_id for item in batch.items),
                    current_run_id=batch.run_id,
                )
            context, decisions = loaded_context, loaded_decisions
        except (
            postgres_suggestions.SuggestionStoreError,
            PostgreSQLRuntimeError,
            psycopg.Error,
        ):
            for index in requested:
                statuses[index] = "suggestion_storage_failed"

    # Failed context transactions are not authoritative: preserve existing rows.
    if requested and context is None:
        return 0, tuple(statuses)

    selected_by_occurrence: dict[
        FindingOccurrenceId, domain_suggestions.ValidatedSuggestion
    ] = {}
    if context is not None:
        changed_by_path = {
            resolve_finding_path(item.path): item for item in changed_files
        }
        moment = datetime.now(timezone.utc)
        candidates: list[domain_suggestions.SuggestionCandidate] = []
        for index in requested:
            finding = findings[index]
            recorded = batch.items[index]
            path = resolve_finding_path(finding.path)
            changed_file = changed_by_path[path]
            decision = decisions.get(recorded.finding_id)
            context_hash = _postgres_context_hash(changed_file, head_sha)
            candidates.append(
                domain_suggestions.SuggestionCandidate(
                    index=index,
                    priority=_postgres_suggestion_rank(finding, index),
                    repository=context.repository,
                    pr_number=context.pr_number,
                    head_sha=context.head_sha,
                    fingerprint=recorded.fingerprint,
                    path=path,
                    finding_line=finding.line,
                    patch=changed_file.patch,
                    raw=suggestions[index],
                    canonical=context.canonical_by_finding_id.get(
                        recorded.finding_id
                    ),
                    suppressed=decision is not None
                    and decision.is_active(current_context_hash=context_hash, now=moment),
                    rule_id=finding.rule_id,
                    category=finding.category,
                    symbol=finding.symbol,
                    anchor=finding.anchor,
                    title=finding.title,
                    evidence=finding.evidence,
                    impact=finding.impact,
                    smallest_fix=finding.smallest_fix,
                )
            )
        selection = domain_suggestions.select_suggestions(
            candidates, head_file_loader=head_file_loader
        )
        for index, status in selection.statuses.items():
            statuses[index] = status
        selected_by_occurrence = {
            batch.items[index].occurrence_id: suggestion
            for index, suggestion in selection.selected.items()
        }

    try:
        with runtime.transaction() as connection:
            postgres_suggestions.replace_suggestions(
                connection, batch=batch, selected=selected_by_occurrence
            )
    except (
        postgres_suggestions.SuggestionStoreError,
        PostgreSQLRuntimeError,
        psycopg.Error,
    ):
        for index in requested:
            statuses[index] = "suggestion_storage_failed"
        return 0, tuple(statuses)
    return len(selected_by_occurrence), tuple(statuses)


def record_run_findings_with_suggestions(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    head_sha: str,
    findings: Sequence[FindingInput],
    changed_files: Sequence[ChangedFileContext],
    suggestions: Sequence[object | None],
    head_file_loader: HeadFileLoader | None,
) -> FindingRecordResult:
    """Commit findings first, then best-effort validated optional suggestions."""
    if len(suggestions) != len(findings):
        raise ReviewFindingError("suggestions must align one-to-one with findings")
    batch = record_run_findings(
        runtime,
        run_id=run_id,
        head_sha=head_sha,
        findings=findings,
        changed_files=changed_files,
    )
    recorded, statuses = _record_postgres_optional_suggestions(
        runtime,
        batch=batch,
        head_sha=resolve_context_hash(head_sha),
        findings=findings,
        changed_files=changed_files,
        suggestions=suggestions,
        head_file_loader=head_file_loader,
    )
    return FindingRecordResult(
        batch=batch,
        suggestions_recorded=recorded,
        suggestion_statuses=statuses,
    )


def _finding_input(item: Mapping[str, object]) -> FindingInput:
    def text(field: str) -> str:
        value = item.get(field)
        if not isinstance(value, str):
            raise ReviewFindingError(f"finding {field} must be a string")
        return value

    def integer(field: str) -> int:
        value = item.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ReviewFindingError(f"finding {field} must be an integer")
        return value

    confidence = item.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ReviewFindingError("finding confidence must be a number")
    introduced = item.get("introduced_by_diff")
    if not isinstance(introduced, bool):
        raise ReviewFindingError("finding introduced_by_diff must be a boolean")
    return FindingInput(
        rule_id=text("rule_id"),
        path=text("path"),
        line=integer("line"),
        symbol=text("symbol"),
        anchor=text("anchor"),
        title=text("title"),
        severity=text("severity"),
        category=text("category"),
        publication_score=integer("publication_score"),
        confidence=float(confidence),
        evidence=text("evidence"),
        disproof_checks=text("disproof_checks"),
        impact=text("impact"),
        smallest_fix=text("smallest_fix"),
        introduced_by_diff=introduced,
    )


def validate_live_findings(
    *,
    head_sha: str,
    raw_findings: Sequence[Mapping[str, object]],
    changed_files: Sequence[ChangedFileContext],
) -> None:
    """Check a tool payload exactly as recording would, without persisting it."""
    resolve_finding_batch(
        head_sha=head_sha,
        findings=tuple(_finding_input(item) for item in raw_findings),
        changed_files=changed_files,
    )


def record_live_findings(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    head_sha: str,
    raw_findings: Sequence[Mapping[str, object]],
    changed_files: Sequence[ChangedFileContext],
    head_file_loader: HeadFileLoader | None,
) -> LiveFindingRecordResult:
    """Validate the tool payload and return its stable PostgreSQL receipt."""
    findings = tuple(_finding_input(item) for item in raw_findings)
    suggestions = tuple(item.get("suggestion") for item in raw_findings)
    result = record_run_findings_with_suggestions(
        runtime,
        run_id=run_id,
        head_sha=head_sha,
        findings=findings,
        changed_files=changed_files,
        suggestions=suggestions,
        head_file_loader=head_file_loader,
    )
    with runtime.transaction() as connection:
        decisions = postgres_decisions.latest_suppression_decisions(
            connection,
            finding_ids=tuple(item.finding_id for item in result.batch.items),
            current_run_id=run_id,
        )
    changed_by_path = {
        resolve_finding_path(item.path): item for item in changed_files
    }
    moment = datetime.now(timezone.utc)
    items: list[LiveRecordedFinding] = []
    for finding, recorded, suggestion_status in zip(
        findings,
        result.batch.items,
        result.suggestion_statuses,
        strict=True,
    ):
        context_hash = _postgres_context_hash(
            changed_by_path[resolve_finding_path(finding.path)], head_sha
        )
        decision = decisions.get(recorded.finding_id)
        suppressed = decision is not None and decision.is_active(current_context_hash=context_hash, now=moment)
        items.append(
            LiveRecordedFinding(
                finding_id=int(recorded.finding_id),
                occurrence_id=int(recorded.occurrence_id),
                fingerprint=recorded.fingerprint,
                local_reference=recorded.local_reference,
                context_hash=context_hash,
                suppressed=suppressed,
                decision=(
                    decision.latest.decision.value
                    if suppressed and decision
                    else None
                ),
                suggestion_status=suggestion_status,
            )
        )
    return LiveFindingRecordResult(
        items=tuple(items),
        suggestions_recorded=result.suggestions_recorded,
    )


def load_live_context(
    runtime: PostgreSQLRuntime, query: FindingContextQuery
) -> FindingHistoryContext:
    """Load bounded historical hints from the only active PostgreSQL store."""
    clean_paths = sorted(
        {resolve_finding_path(path) for path in query.paths if path.strip()}
    )
    moment = datetime.now(timezone.utc)
    with runtime.transaction() as connection:
        repository = postgres_reporting.repository_scope(
            connection, repository=query.repository
        )
        recent_reports = postgres_reporting.list_finding_context(
            connection,
            repository_id=repository.id,
            paths=clean_paths,
            limit=30,
            now=moment,
        )

        repeats: tuple[RepeatFinding, ...] = ()
        suppressed_repeat_fingerprints = frozenset[str]()
        active_publication_fingerprints: set[str] | None = None
        if query.pr_number is not None:
            active = connection.execute(
                """
                SELECT run.id
                FROM review_agent.review_runs AS run
                JOIN review_agent.pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                JOIN review_agent.repositories AS repository
                  ON repository.id = pull_request.repository_id
                WHERE lower(repository.full_name) = lower(%s)
                  AND pull_request.number = %s AND run.status = 'running' AND run.purpose = 'code'
                """,
                (query.repository, query.pr_number),
            ).fetchone()
            if active is not None:
                repeats = postgres_findings.repeat_history(
                    connection,
                    run_id=ReviewRunId(int(active[0])),
                    limit=MAX_FINDINGS_PER_REVIEW,
                )
                suppressed_repeat_fingerprints = (
                    postgres_reporting.active_repeat_suppressions(
                        connection,
                        repository_id=repository.id,
                        current_run_id=ReviewRunId(int(active[0])),
                        fingerprint_contexts=tuple(
                            (item.fingerprint, item.context_hash) for item in repeats
                        ),
                        now=moment,
                    )
                )
                rows = connection.execute(
                    """
                    SELECT identity.fingerprint
                    FROM review_agent.publications AS publication
                    JOIN review_agent.publication_findings AS item
                      ON item.publication_id = publication.id
                    JOIN review_agent.finding_identities AS identity
                      ON identity.id = item.finding_id
                    WHERE publication.pull_request_id = (
                        SELECT pull_request_id
                        FROM review_agent.review_runs WHERE id = %s
                    )
                      AND publication.status = 'posted' AND publication.purpose = 'code'
                      AND publication.superseded_by_publication_id IS NULL
                      AND item.outcome IN ('current', 'not_checked')
                    """,
                    (int(active[0]),),
                ).fetchall()
                if rows:
                    active_publication_fingerprints = {str(row[0]) for row in rows}

    recent: list[dict[str, object]] = []
    suppressions: list[dict[str, object]] = []
    for item in recent_reports:
        latest_decision = (
            {"decision": item.latest_decision.value}
            if item.latest_decision is not None
            else None
        )
        recent.append(
            {
                "fingerprint": item.fingerprint,
                "rule_id": item.rule_id,
                "path": item.path,
                "line": item.line,
                "symbol": item.symbol,
                "anchor": item.anchor,
                "title": item.title,
                "severity": item.severity.value,
                "category": item.category.value,
                "publication_score": item.publication_score,
                "confidence": item.confidence,
                "context_hash": item.context_hash,
                "last_seen_at": item.last_seen_at.isoformat(),
                "evidence": item.evidence,
                "disproof_checks": item.disproof_checks,
                "impact": item.impact,
                "smallest_fix": item.smallest_fix,
                "suppressed_for_last_seen_file_version": item.suppressed,
                "latest_decision": latest_decision,
            }
        )
        if item.suppressed and item.latest_decision is not None:
            suppressions.append(
                {
                    "fingerprint": item.fingerprint,
                    "rule_id": item.rule_id,
                    "path": item.path,
                    "symbol": item.symbol,
                    "anchor": item.anchor,
                    "decision": item.latest_decision.value,
                    "warning": (
                        "Historical hint only; the record tool rechecks the exact current context."
                    ),
                }
            )

    repeat_payloads: list[dict[str, object]] = []
    for item in repeats:
        if (
            active_publication_fingerprints is not None
            and item.fingerprint not in active_publication_fingerprints
        ):
            continue
        # The publication planner records the matching prior item as suppressed.
        if item.fingerprint in suppressed_repeat_fingerprints:
            continue
        repeat_payloads.append(
            {
                "fingerprint": item.fingerprint,
                "local_reference": item.local_reference,
                "previous_run_id": int(item.previous_run_id),
                "previous_head": item.previous_head,
                "rule_id": item.rule_id,
                "path": item.path,
                "line": item.line,
                "symbol": item.symbol,
                "anchor": item.anchor,
                "title": item.title,
                "severity": item.severity.value,
                "category": item.category.value,
                "publication_score": item.publication_score,
                "confidence": item.confidence,
                "context_hash": item.context_hash,
                "prior_claim": item.prior_claim,
                "prior_disproof_checks": item.prior_disproof_checks,
                "prior_impact": item.prior_impact,
                "prior_smallest_fix": item.prior_smallest_fix,
                "suppressed_for_last_seen_file_version": False,
                "latest_decision": None,
            }
        )

    return FindingHistoryContext(
        repository=query.repository,
        paths=clean_paths,
        policy=(
            "Human decisions are historical hints during analysis. The record tool "
            "suppresses only when the exact current context matches the reviewed context."
        ),
        historical_suppressions=suppressions,
        recent_findings=recent,
        repeat_review_findings=repeat_payloads,
    )
