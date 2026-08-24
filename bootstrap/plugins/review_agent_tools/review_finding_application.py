"""Historical finding context, persistence, and atomic-suggestion coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
import sqlite3
from typing import TypeAlias, TypedDict, cast

import psycopg

from . import (
    memory_findings,
    memory_schema,
    memory_suggestions,
    memory_validation,
    suggestion_validation,
)
from .domain.finding import (
    MAX_FINDINGS_PER_REVIEW,
    FindingDefinition,
    FindingDecision,
    FindingDomainError,
    FindingId,
    FindingInput,
    FindingOccurrenceId,
    RepeatFinding,
    resolve_context_hash,
    resolve_finding,
    resolve_decision,
    resolve_finding_path,
    resolve_fingerprint_query,
    require_unique_finding_identities,
    suppression_is_active,
)
from .domain.review import RepositoryId, ReviewRunId
from .postgres import (
    decisions as postgres_decisions,
    findings as postgres_findings,
    suggestions as postgres_suggestions,
)
from .postgres.runtime import (
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HeadFileLoader = Callable[[str], str | None]
RecordedFinding = dict[str, object]
PostgresFindingBatch: TypeAlias = postgres_findings.FindingBatch
DecisionAudit: TypeAlias = postgres_decisions.DecisionAudit


class ReviewFindingError(ValueError):
    """A finding does not belong to the changed files of its review snapshot."""


class FindingMemoryContext(TypedDict):
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
class FindingRecordSubject:
    repository: str
    pr_number: int
    run_id: int
    base_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    context_hash: str = ""
    context_hash_source: str = ""
    patch: str | None = None


@dataclass(frozen=True, slots=True)
class FindingRecordResult:
    items: list[RecordedFinding]
    suggestions_recorded: int


@dataclass(frozen=True, slots=True)
class PostgresFindingRecordResult:
    batch: PostgresFindingBatch
    suggestions_recorded: int
    suggestion_statuses: tuple[suggestion_validation.SuggestionStatus, ...]


def _postgres_context_hash(changed_file: ChangedFile, head_sha: str) -> str:
    candidate = changed_file.context_hash.strip().lower()
    if changed_file.context_hash_source == "blob" and _SHA_RE.fullmatch(candidate):
        return resolve_context_hash(candidate)
    return head_sha


def record_postgres_findings(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    head_sha: str,
    findings: Sequence[FindingInput],
    changed_files: Sequence[ChangedFile],
) -> PostgresFindingBatch:
    """Validate and atomically persist one pre-reviewed PostgreSQL finding batch."""
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
    with runtime.transaction() as connection:
        return postgres_findings.record_findings(
            connection,
            run_id=run_id,
            expected_head_sha=resolved_head,
            definitions=tuple(definitions),
        )


def resolve_postgres_fingerprint(
    runtime: PostgreSQLRuntime,
    *,
    repository_id: RepositoryId,
    value: str,
) -> str:
    """Resolve a fingerprint only within the caller's stable repository identity."""
    query = resolve_fingerprint_query(value)
    with runtime.transaction() as connection:
        return postgres_findings.resolve_fingerprint(
            connection, repository_id=repository_id, query=query
        )


def load_postgres_repeat_history(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
) -> tuple[RepeatFinding, ...]:
    """Load bounded prior occurrence context for one exact pull request."""
    with runtime.transaction() as connection:
        return postgres_findings.repeat_history(
            connection, run_id=run_id, limit=MAX_FINDINGS_PER_REVIEW
        )


def _postgres_suggestion_rank(
    finding: FindingInput, index: int
) -> tuple[int, int, str, int]:
    return (
        int(memory_validation.SEVERITY_PRIORITY.get(finding.severity.title(), 99)),
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
    changed_files: Sequence[ChangedFile],
    suggestions: Sequence[object | None],
    head_file_loader: HeadFileLoader | None,
) -> tuple[int, tuple[suggestion_validation.SuggestionStatus, ...]]:
    requested = [index for index, item in enumerate(suggestions) if item is not None]
    statuses: list[suggestion_validation.SuggestionStatus] = [
        "not_requested"
    ] * len(findings)
    context: postgres_suggestions.SuggestionContext | None = None
    decisions: Mapping[FindingId, FindingDecision] = {}
    if requested:
        try:
            with runtime.transaction() as connection:
                loaded_context = postgres_suggestions.load_context(
                    connection, batch
                )
                loaded_decisions = postgres_decisions.latest_decisions(
                    connection,
                    finding_ids=tuple(item.finding_id for item in batch.items),
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
        FindingOccurrenceId, suggestion_validation.ValidatedSuggestion
    ] = {}
    if context is not None:
        changed_by_path = {
            resolve_finding_path(item.path): item for item in changed_files
        }
        moment = datetime.now(timezone.utc)
        candidates: list[suggestion_validation.SuggestionCandidate] = []
        for index in requested:
            finding = findings[index]
            recorded = batch.items[index]
            path = resolve_finding_path(finding.path)
            changed_file = changed_by_path[path]
            decision = decisions.get(recorded.finding_id)
            context_hash = _postgres_context_hash(changed_file, head_sha)
            candidates.append(
                suggestion_validation.SuggestionCandidate(
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
                    and suppression_is_active(
                        decision=decision.decision,
                        decision_context_hash=decision.context_hash,
                        current_context_hash=context_hash,
                        expires_at=decision.expires_at,
                        now=moment,
                    ),
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
        selection = suggestion_validation.select_suggestions(
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


def record_postgres_findings_with_suggestions(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    head_sha: str,
    findings: Sequence[FindingInput],
    changed_files: Sequence[ChangedFile],
    suggestions: Sequence[object | None],
    head_file_loader: HeadFileLoader | None,
) -> PostgresFindingRecordResult:
    """Commit findings first, then best-effort validated optional suggestions."""
    if len(suggestions) != len(findings):
        raise ReviewFindingError("suggestions must align one-to-one with findings")
    batch = record_postgres_findings(
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
    return PostgresFindingRecordResult(
        batch=batch,
        suggestions_recorded=recorded,
        suggestion_statuses=statuses,
    )


def _validated_decision_audit(audit: DecisionAudit) -> DecisionAudit:
    actor_user_id = audit.actor_user_id.strip()
    if not actor_user_id or len(actor_user_id) > 200:
        raise ReviewFindingError("decision audit actor_user_id is invalid")
    actor_login = audit.actor_login.strip() if audit.actor_login else None
    author_association = (
        audit.author_association.strip() if audit.author_association else None
    )
    if actor_login and len(actor_login) > 200:
        raise ReviewFindingError("decision audit actor_login is invalid")
    if author_association and len(author_association) > 80:
        raise ReviewFindingError("decision audit author_association is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", audit.allowlist_version):
        raise ReviewFindingError("decision audit allowlist_version is invalid")
    if isinstance(audit.source_comment_id, bool) or audit.source_comment_id < 1:
        raise ReviewFindingError("decision audit source_comment_id is invalid")
    source_url = audit.source_comment_url.strip() if audit.source_comment_url else None
    if source_url and ("\x00" in source_url or len(source_url) > 1_000):
        raise ReviewFindingError("decision audit source_comment_url is invalid")
    return DecisionAudit(
        actor_user_id=actor_user_id,
        actor_login=actor_login,
        author_association=author_association,
        allowlist_version=audit.allowlist_version,
        source_comment_id=audit.source_comment_id,
        source_comment_url=source_url,
    )


def append_postgres_governance_decision(
    runtime: PostgreSQLRuntime,
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    decision: str,
    reason: str,
    actor: str,
    audit: DecisionAudit,
    expires_days: int | None = None,
    adr_id: str = "",
    now: datetime | None = None,
) -> FindingDecision:
    """Append one context-derived human decision and its audit atomically."""
    moment = now or datetime.now(timezone.utc)
    definition = resolve_decision(
        decision=decision,
        reason=reason,
        actor=actor,
        adr_id=adr_id,
        expires_days=expires_days,
        now=moment,
    )
    clean_audit = _validated_decision_audit(audit)
    with runtime.transaction() as connection:
        return postgres_decisions.append_decision_with_audit(
            connection,
            finding_id=finding_id,
            occurrence_id=occurrence_id,
            definition=definition,
            audit=clean_audit,
        )


def load_postgres_active_suppression(
    runtime: PostgreSQLRuntime,
    *,
    finding_id: FindingId,
    context_hash: str,
    now: datetime | None = None,
) -> FindingDecision | None:
    """Return an unexpired suppressive decision for the exact current context."""
    current_context_hash = resolve_context_hash(context_hash)
    with runtime.transaction() as connection:
        decision = postgres_decisions.latest_decision(
            connection, finding_id=finding_id
        )
    if decision is None:
        return None
    return decision if suppression_is_active(
        decision=decision.decision,
        decision_context_hash=decision.context_hash,
        current_context_hash=current_context_hash,
        expires_at=decision.expires_at,
        now=now or datetime.now(timezone.utc),
    ) else None


def load_context(query: FindingContextQuery) -> FindingMemoryContext:
    """Load bounded historical context for one repository and optional pull request."""
    with closing(memory_schema.connect_existing()) as connection:
        context = memory_findings.memory_context(
            connection,
            query.repository,
            query.paths,
            pr_number=query.pr_number,
        )
    return cast(FindingMemoryContext, context)


def _string_field(item: Mapping[str, object], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str):
        raise ReviewFindingError(f"recorded finding {field} is invalid")
    return value


def _integer_field(item: Mapping[str, object], field: str) -> int:
    value = item.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReviewFindingError(f"recorded finding {field} is invalid")
    return value


def _suggestion_rank(
    finding: Mapping[str, object], index: int
) -> tuple[int, int, str, int]:
    severity = str(finding.get("severity", "")).strip().title()
    publication_score_value = finding.get("publication_score", 0)
    try:
        publication_score = int(cast(int | str, publication_score_value))
    except (TypeError, ValueError):
        publication_score = 0
    return (
        int(memory_validation.SEVERITY_PRIORITY.get(severity, 99)),
        -publication_score,
        str(finding.get("rule_id", "")),
        index,
    )


def _clear_suggestions(recorded: Sequence[RecordedFinding]) -> None:
    with closing(memory_schema.connect_existing()) as connection, connection:
        for item in recorded:
            memory_suggestions.replace_observation_suggestion(
                connection,
                observation_id=_integer_field(item, "observation_id"),
                suggestion=None,
            )


def _store_suggestions(
    recorded: Sequence[RecordedFinding],
    selected: Mapping[int, suggestion_validation.ValidatedSuggestion],
) -> None:
    with closing(memory_schema.connect_existing()) as connection, connection:
        for item in recorded:
            memory_suggestions.replace_observation_suggestion(
                connection,
                observation_id=_integer_field(item, "observation_id"),
                suggestion=None,
            )
        for index, suggestion in selected.items():
            memory_suggestions.replace_observation_suggestion(
                connection,
                observation_id=_integer_field(recorded[index], "observation_id"),
                suggestion=suggestion,
            )


def _record_optional_suggestions(
    subject: FindingRecordSubject,
    findings: Sequence[Mapping[str, object]],
    recorded: list[RecordedFinding],
    changed_by_path: Mapping[str, ChangedFile],
    head_file_loader: HeadFileLoader | None,
) -> tuple[int, dict[int, suggestion_validation.SuggestionStatus]]:
    requested = [
        index for index, finding in enumerate(findings) if "suggestion" in finding
    ]
    if not requested:
        _clear_suggestions(recorded)
        return 0, {}

    try:
        key_by_index = {
            index: suggestion_validation.suggestion_key(
                subject.repository,
                subject.pr_number,
                subject.head_sha,
                _string_field(recorded[index], "fingerprint"),
            )
            for index in requested
        }
        with closing(memory_schema.connect_existing()) as connection:
            canonical_by_key = memory_suggestions.canonical_suggestions(
                connection, key_by_index.values()
            )
    except (memory_validation.ReviewMemoryError, sqlite3.Error):
        return 0, {index: "suggestion_storage_failed" for index in requested}

    candidates: list[suggestion_validation.SuggestionCandidate] = []
    for index in requested:
        finding = findings[index]
        path = _string_field(recorded[index], "path")
        candidates.append(
            suggestion_validation.SuggestionCandidate(
                index=index,
                priority=_suggestion_rank(finding, index),
                repository=subject.repository,
                pr_number=subject.pr_number,
                head_sha=subject.head_sha,
                fingerprint=_string_field(recorded[index], "fingerprint"),
                path=path,
                finding_line=_integer_field(finding, "line"),
                patch=changed_by_path[path].patch,
                raw=finding.get("suggestion"),
                canonical=canonical_by_key.get(key_by_index[index]),
                suppressed=bool(recorded[index].get("suppressed", False)),
                rule_id=str(finding.get("rule_id", "")),
                category=str(finding.get("category", "")),
                symbol=str(finding.get("symbol", "")),
                anchor=str(finding.get("anchor", "")),
                title=str(finding.get("title", "")),
                evidence=str(finding.get("evidence", "")),
                impact=str(finding.get("impact", "")),
                smallest_fix=str(finding.get("smallest_fix", "")),
            )
        )
    selection = suggestion_validation.select_suggestions(
        candidates, head_file_loader=head_file_loader
    )
    statuses: dict[int, suggestion_validation.SuggestionStatus] = dict(
        selection.statuses
    )
    selected = selection.selected

    try:
        _store_suggestions(recorded, selected)
    except (memory_validation.ReviewMemoryError, sqlite3.Error):
        for index in requested:
            statuses[index] = "suggestion_storage_failed"
        return 0, statuses
    return len(selected), statuses


def record_findings(
    subject: FindingRecordSubject,
    *,
    findings: Sequence[Mapping[str, object]],
    changed_files: Sequence[ChangedFile],
    head_file_loader: HeadFileLoader | None,
) -> FindingRecordResult:
    """Persist findings and replace their optional same-head atomic suggestions."""
    changed_by_path = {
        memory_validation.normalize_path(item.path): item for item in changed_files
    }
    context_hashes: dict[str, str] = {}
    for finding in findings:
        path = memory_validation.normalize_path(str(finding.get("path", "")))
        changed_file = changed_by_path.get(path)
        if changed_file is None:
            raise ReviewFindingError(
                "every recorded finding must point to a changed pull-request file"
            )
        candidate_hash = changed_file.context_hash.strip().lower()
        context_hashes[path] = (
            candidate_hash
            if changed_file.context_hash_source == "blob"
            and _SHA_RE.fullmatch(candidate_hash)
            else subject.head_sha
        )

    with closing(memory_schema.connect_existing()) as connection:
        raw_recorded = memory_findings.record_findings(
            connection,
            subject.repository,
            subject.pr_number,
            subject.head_sha,
            findings,
            review_run_id=subject.run_id,
            base_sha=subject.base_sha,
            context_hashes=context_hashes,
        )
    recorded = cast(list[RecordedFinding], raw_recorded)
    suggestions_recorded, statuses = _record_optional_suggestions(
        subject,
        findings,
        recorded,
        changed_by_path,
        head_file_loader,
    )
    for index, reason in statuses.items():
        recorded[index]["suggestion"] = (
            {"status": "recorded"}
            if reason == "recorded"
            else {"status": "omitted", "reason": reason}
        )
    return FindingRecordResult(
        items=recorded,
        suggestions_recorded=suggestions_recorded,
    )
