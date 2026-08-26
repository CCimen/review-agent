"""Validate and select optional, head-scoped atomic suggestions."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, TypedDict, cast

try:
    from . import diff_render
    from .memory_validation import normalize_path, normalize_repository
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import diff_render  # type: ignore[no-redef]
    from memory_validation import (  # type: ignore[no-redef]
        normalize_path,
        normalize_repository,
    )


MAX_ATOMIC_SUGGESTIONS_PER_REVIEW = 12
MAX_SUGGESTION_RANGE_LINES = 8
MAX_SUGGESTION_REPLACEMENT_LINES = 16
MAX_SUGGESTION_TEXT_CHARS = 2_400
SUGGESTION_MARKER_PREFIX = "review-agent:suggestion key="

_SUGGESTION_FIELDS = frozenset(
    {"start_line", "end_line", "expected_text", "replacement_text"}
)
_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_PLACEHOLDER_RE = re.compile(
    r"(?im)(?:^|\W)(?:TODO|FIXME|TBD)(?:\W|$)|"
    r"<[^>\n]*(?:placeholder|insert here|replace me)[^>\n]*>"
)
_HIGH_RISK_FINDING_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:"
    r"auth(?:entication|orization)?|oauth|oidc|rbac|acl|tenant|permission|"
    r"migration|migrate|alembic|openapi|contract|schema|serializ(?:e|ation)|"
    r"persist(?:ed|ence)?|database|lifecycle|generated"
    r")(?:[^a-z0-9]|$)"
)


class ValidatedSuggestion(TypedDict):
    path: str
    start_line: int
    end_line: int
    expected_hash: str
    replacement_text: str
    suggestion_key: str


SuggestionStatus = Literal[
    "not_requested",
    "recorded",
    "suggestion_contains_placeholder",
    "suggestion_expected_text_mismatch",
    "suggestion_fields_invalid",
    "suggestion_finding_suppressed",
    "suggestion_has_no_change",
    "suggestion_head_file_unavailable",
    "suggestion_head_repository_unavailable",
    "suggestion_high_risk_category",
    "suggestion_high_risk_domain",
    "suggestion_must_be_an_object",
    "suggestion_must_include_finding_line",
    "suggestion_overlaps_higher_priority_patch",
    "suggestion_range_invalid",
    "suggestion_range_not_in_changed_hunk",
    "suggestion_range_outside_head_file",
    "suggestion_range_too_large",
    "suggestion_replacement_too_large",
    "suggestion_review_limit",
    "suggestion_storage_failed",
    "suggestion_text_invalid",
    "suggestion_validation_failed",
]


@dataclass(frozen=True)
class SuggestionValidation:
    suggestion: ValidatedSuggestion | None
    rejection_reason: SuggestionStatus | Literal[""]


@dataclass(frozen=True, slots=True)
class SuggestionCandidate:
    index: int
    priority: tuple[int, int, str, int]
    repository: str
    pr_number: int
    head_sha: str
    fingerprint: str
    path: str
    finding_line: int
    patch: str | None
    raw: object
    canonical: ValidatedSuggestion | None
    suppressed: bool
    rule_id: str
    category: str
    symbol: str
    anchor: str
    title: str
    evidence: str
    impact: str
    smallest_fix: str


@dataclass(frozen=True, slots=True)
class SuggestionSelection:
    selected: Mapping[int, ValidatedSuggestion]
    statuses: Mapping[int, SuggestionStatus]


def _positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if value > 0 else None


def _canonical_code_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        return None
    if len(text) > MAX_SUGGESTION_TEXT_CHARS:
        return None
    if (
        "```" in text
        or SUGGESTION_MARKER_PREFIX in text
        or any(character in _BIDI_CONTROLS for character in text)
    ):
        return None
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or ord(character) == 127
        for character in text
    ):
        return None
    return text


def suggestion_eligibility_rejection(
    *,
    rule_id: str,
    category: str,
    path: str,
    symbol: str,
    anchor: str,
    title: str,
    evidence: str,
    impact: str,
    smallest_fix: str,
) -> SuggestionStatus | Literal[""]:
    """Fail closed for high-risk domains that must never get one-click patches."""
    if category.strip().casefold().replace("_", "-") in {
        "security",
        "privacy",
        "migration",
        "migrations",
        "contract",
        "contracts",
        "api",
        "data-contract",
        "database",
        "persistence",
        "generated",
    }:
        return "suggestion_high_risk_category"
    searchable = "\n".join(
        (rule_id, path, symbol, anchor, title, evidence, impact, smallest_fix)
    )
    if _HIGH_RISK_FINDING_RE.search(searchable):
        return "suggestion_high_risk_domain"
    return ""


def suggestion_key(
    repository: str,
    pr_number: int,
    head_sha: str,
    fingerprint: str,
) -> str:
    """Return one stable identity per finding on an exact PR head."""
    payload = json.dumps(
        {
            "repository": normalize_repository(repository),
            "pr_number": int(pr_number),
            "head_sha": head_sha.lower(),
            "fingerprint": fingerprint.lower(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_suggestion(
    raw: object,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    fingerprint: str,
    path: str,
    finding_line: int,
    patch: str | None,
    head_text: str,
    head_text_start_line: int = 1,
) -> SuggestionValidation:
    """Validate an optional model patch against trusted head bytes and diff lines."""
    if not isinstance(raw, Mapping):
        return SuggestionValidation(None, "suggestion_must_be_an_object")
    raw_mapping = cast(Mapping[str, object], raw)
    if frozenset(raw_mapping) != _SUGGESTION_FIELDS:
        return SuggestionValidation(None, "suggestion_fields_invalid")

    start_line = _positive_int(raw_mapping.get("start_line"))
    end_line = _positive_int(raw_mapping.get("end_line"))
    if start_line is None or end_line is None or end_line < start_line:
        return SuggestionValidation(None, "suggestion_range_invalid")
    if end_line - start_line + 1 > MAX_SUGGESTION_RANGE_LINES:
        return SuggestionValidation(None, "suggestion_range_too_large")
    if not start_line <= int(finding_line) <= end_line:
        return SuggestionValidation(None, "suggestion_must_include_finding_line")
    if not diff_render.is_suggestible_right_side_range(
        patch, start_line=start_line, end_line=end_line
    ):
        return SuggestionValidation(None, "suggestion_range_not_in_changed_hunk")

    expected = _canonical_code_text(raw_mapping.get("expected_text"))
    replacement = _canonical_code_text(raw_mapping.get("replacement_text"))
    if expected is None or replacement is None:
        return SuggestionValidation(None, "suggestion_text_invalid")
    replacement_lines = 0 if not replacement else replacement.count("\n") + 1
    if replacement_lines > MAX_SUGGESTION_REPLACEMENT_LINES:
        return SuggestionValidation(None, "suggestion_replacement_too_large")
    if replacement.strip() in {"...", "…"} or _PLACEHOLDER_RE.search(replacement):
        return SuggestionValidation(None, "suggestion_contains_placeholder")

    trusted_lines = head_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    start_index = start_line - head_text_start_line
    end_index = end_line - head_text_start_line + 1
    if start_index < 0 or end_index > len(trusted_lines):
        return SuggestionValidation(None, "suggestion_range_outside_head_file")
    trusted_text = "\n".join(trusted_lines[start_index:end_index])
    if expected != trusted_text:
        return SuggestionValidation(None, "suggestion_expected_text_mismatch")
    if replacement == trusted_text:
        return SuggestionValidation(None, "suggestion_has_no_change")

    return SuggestionValidation(
        {
            "path": normalize_path(path),
            "start_line": start_line,
            "end_line": end_line,
            "expected_hash": hashlib.sha256(trusted_text.encode("utf-8")).hexdigest(),
            "replacement_text": replacement,
            "suggestion_key": suggestion_key(
                repository, pr_number, head_sha, fingerprint
            ),
        },
        "",
    )


def ranges_overlap(
    first: ValidatedSuggestion, second: ValidatedSuggestion
) -> bool:
    """Return whether two candidate edits intersect in the same file."""
    return first["path"] == second["path"] and not (
        first["end_line"] < second["start_line"]
        or second["end_line"] < first["start_line"]
    )


def select_suggestions(
    candidates: Sequence[SuggestionCandidate],
    *,
    head_file_loader: Callable[[str, int, int], str | None] | None,
) -> SuggestionSelection:
    """Select a bounded, non-overlapping patch set without database access."""
    selected: dict[int, ValidatedSuggestion] = {}
    statuses: dict[int, SuggestionStatus] = {}
    head_ranges: dict[tuple[str, int, int], str | None] = {}
    for candidate in sorted(candidates, key=lambda item: item.priority):
        if candidate.suppressed:
            statuses[candidate.index] = "suggestion_finding_suppressed"
            continue
        rejection = suggestion_eligibility_rejection(
            rule_id=candidate.rule_id,
            category=candidate.category,
            path=candidate.path,
            symbol=candidate.symbol,
            anchor=candidate.anchor,
            title=candidate.title,
            evidence=candidate.evidence,
            impact=candidate.impact,
            smallest_fix=candidate.smallest_fix,
        )
        if rejection:
            statuses[candidate.index] = rejection
            continue
        if len(selected) >= MAX_ATOMIC_SUGGESTIONS_PER_REVIEW:
            statuses[candidate.index] = "suggestion_review_limit"
            continue

        suggestion = candidate.canonical
        if suggestion is not None and suggestion["path"] != candidate.path:
            statuses[candidate.index] = "suggestion_validation_failed"
            continue
        if suggestion is None:
            if head_file_loader is None:
                statuses[candidate.index] = (
                    "suggestion_head_repository_unavailable"
                )
                continue
            raw = candidate.raw
            raw_mapping: Mapping[str, object] = (
                cast(Mapping[str, object], raw)
                if isinstance(raw, Mapping)
                else cast(Mapping[str, object], {})
            )
            start_line = _positive_int(raw_mapping.get("start_line"))
            end_line = _positive_int(raw_mapping.get("end_line"))
            if start_line is None or end_line is None:
                validation = validate_suggestion(
                    candidate.raw,
                    repository=candidate.repository,
                    pr_number=candidate.pr_number,
                    head_sha=candidate.head_sha,
                    fingerprint=candidate.fingerprint,
                    path=candidate.path,
                    finding_line=candidate.finding_line,
                    patch=candidate.patch,
                    head_text="",
                )
                statuses[candidate.index] = (
                    validation.rejection_reason
                    if validation.rejection_reason
                    else "suggestion_validation_failed"
                )
                continue
            range_key = (candidate.path, start_line, end_line)
            if range_key not in head_ranges:
                if len(head_ranges) >= MAX_ATOMIC_SUGGESTIONS_PER_REVIEW:
                    statuses[candidate.index] = "suggestion_review_limit"
                    continue
                try:
                    head_ranges[range_key] = head_file_loader(
                        candidate.path, start_line, end_line
                    )
                except Exception:
                    # Provider reads are best-effort and must not lose the finding batch.
                    head_ranges[range_key] = None
            head_text = head_ranges[range_key]
            if head_text is None:
                statuses[candidate.index] = "suggestion_head_file_unavailable"
                continue
            try:
                validation = validate_suggestion(
                    candidate.raw,
                    repository=candidate.repository,
                    pr_number=candidate.pr_number,
                    head_sha=candidate.head_sha,
                    fingerprint=candidate.fingerprint,
                    path=candidate.path,
                    finding_line=candidate.finding_line,
                    patch=candidate.patch,
                    head_text=head_text,
                    head_text_start_line=start_line,
                )
            except ValueError:
                statuses[candidate.index] = "suggestion_validation_failed"
                continue
            if validation.suggestion is None:
                statuses[candidate.index] = (
                    validation.rejection_reason
                    if validation.rejection_reason
                    else "suggestion_validation_failed"
                )
                continue
            suggestion = validation.suggestion

        if any(ranges_overlap(suggestion, item) for item in selected.values()):
            statuses[candidate.index] = (
                "suggestion_overlaps_higher_priority_patch"
            )
            continue
        selected[candidate.index] = suggestion
        statuses[candidate.index] = "recorded"
    return SuggestionSelection(selected=selected, statuses=statuses)
