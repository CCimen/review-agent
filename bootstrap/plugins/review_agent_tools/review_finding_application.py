"""Historical finding context, persistence, and atomic-suggestion coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
import re
import sqlite3
from typing import TypedDict, cast

from . import memory_db, memory_suggestions


_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HeadFileLoader = Callable[[str], str | None]
RecordedFinding = dict[str, object]


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


def load_context(query: FindingContextQuery) -> FindingMemoryContext:
    """Load bounded historical context for one repository and optional pull request."""
    with closing(memory_db.connect_existing()) as connection:
        context = memory_db.memory_context(
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
        int(memory_db.SEVERITY_PRIORITY.get(severity, 99)),
        -publication_score,
        str(finding.get("rule_id", "")),
        index,
    )


def _ranges_overlap(
    first: memory_suggestions.ValidatedSuggestion,
    second: memory_suggestions.ValidatedSuggestion,
) -> bool:
    return first["path"] == second["path"] and not (
        first["end_line"] < second["start_line"]
        or second["end_line"] < first["start_line"]
    )


def _clear_suggestions(recorded: Sequence[RecordedFinding]) -> None:
    with closing(memory_db.connect_existing()) as connection, connection:
        for item in recorded:
            memory_suggestions.replace_observation_suggestion(
                connection,
                observation_id=_integer_field(item, "observation_id"),
                suggestion=None,
            )


def _store_suggestions(
    recorded: Sequence[RecordedFinding],
    selected: Mapping[int, memory_suggestions.ValidatedSuggestion],
) -> None:
    with closing(memory_db.connect_existing()) as connection, connection:
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
) -> tuple[int, dict[int, str]]:
    requested = [
        index for index, finding in enumerate(findings) if "suggestion" in finding
    ]
    if not requested:
        _clear_suggestions(recorded)
        return 0, {}

    statuses: dict[int, str] = (
        {}
        if head_file_loader is not None
        else {
            index: "suggestion_head_repository_unavailable" for index in requested
        }
    )
    ordered = sorted(requested, key=lambda value: _suggestion_rank(findings[value], value))
    try:
        key_by_index = {
            index: memory_suggestions.suggestion_key(
                subject.repository,
                subject.pr_number,
                subject.head_sha,
                _string_field(recorded[index], "fingerprint"),
            )
            for index in requested
        }
        with closing(memory_db.connect_existing()) as connection:
            canonical_by_key = memory_suggestions.canonical_suggestions(
                connection, key_by_index.values()
            )
    except (memory_db.ReviewMemoryError, sqlite3.Error):
        return 0, {index: "suggestion_storage_failed" for index in requested}

    head_files: dict[str, str | None] = {}
    selected: dict[int, memory_suggestions.ValidatedSuggestion] = {}
    for index in ordered:
        if bool(recorded[index].get("suppressed", False)):
            statuses[index] = "suggestion_finding_suppressed"
            continue
        finding = findings[index]
        eligibility_rejection = memory_suggestions.suggestion_eligibility_rejection(
            rule_id=str(finding.get("rule_id", "")),
            category=str(finding.get("category", "")),
            path=_string_field(recorded[index], "path"),
            symbol=str(finding.get("symbol", "")),
            anchor=str(finding.get("anchor", "")),
            title=str(finding.get("title", "")),
            evidence=str(finding.get("evidence", "")),
            impact=str(finding.get("impact", "")),
            smallest_fix=str(finding.get("smallest_fix", "")),
        )
        if eligibility_rejection:
            statuses[index] = eligibility_rejection
            continue
        if len(selected) >= memory_suggestions.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW:
            statuses[index] = "suggestion_review_limit"
            continue
        path = _string_field(recorded[index], "path")
        canonical = canonical_by_key.get(key_by_index[index])
        if canonical is not None:
            if canonical["path"] != path:
                statuses[index] = "suggestion_validation_failed"
                continue
            if any(_ranges_overlap(canonical, existing) for existing in selected.values()):
                statuses[index] = "suggestion_overlaps_higher_priority_patch"
                continue
            selected[index] = canonical
            statuses[index] = "recorded"
            continue

        if head_file_loader is None:
            continue
        raw_suggestion_value = finding.get("suggestion")
        if not isinstance(raw_suggestion_value, Mapping):
            statuses[index] = "suggestion_must_be_an_object"
            continue
        raw_suggestion = cast(Mapping[str, object], raw_suggestion_value)
        if path not in head_files:
            if (
                len(head_files)
                >= memory_suggestions.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW
            ):
                statuses[index] = "suggestion_review_limit"
                continue
            head_files[path] = head_file_loader(path)
        head_text = head_files[path]
        if head_text is None:
            statuses[index] = "suggestion_head_file_unavailable"
            continue

        changed_file = changed_by_path[path]
        try:
            validation = memory_suggestions.validate_suggestion(
                raw_suggestion,
                repository=subject.repository,
                pr_number=subject.pr_number,
                head_sha=subject.head_sha,
                fingerprint=_string_field(recorded[index], "fingerprint"),
                path=path,
                finding_line=_integer_field(finding, "line"),
                patch=changed_file.patch,
                head_text=head_text,
            )
        except memory_db.ReviewMemoryError:
            statuses[index] = "suggestion_validation_failed"
            continue
        if validation.suggestion is None:
            statuses[index] = validation.rejection_reason
            continue
        candidate = validation.suggestion
        if any(_ranges_overlap(candidate, existing) for existing in selected.values()):
            statuses[index] = "suggestion_overlaps_higher_priority_patch"
            continue
        selected[index] = candidate
        statuses[index] = "recorded"

    try:
        _store_suggestions(recorded, selected)
    except (memory_db.ReviewMemoryError, sqlite3.Error):
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
        memory_db.normalize_path(item.path): item for item in changed_files
    }
    context_hashes: dict[str, str] = {}
    for finding in findings:
        path = memory_db.normalize_path(str(finding.get("path", "")))
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

    with closing(memory_db.connect_existing()) as connection:
        raw_recorded = memory_db.record_findings(
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
