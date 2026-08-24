"""Pure construction of one immutable PostgreSQL publication plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from .domain.publication import (
    JsonValue,
    PublicationFindingInput,
    PublicationFindingOutcome,
    PublicationPartInput,
    PublicationPartType,
    PublicationPlan,
    resolve_publication_plan,
)
from .memory_validation import (
    MAX_FINDINGS_PER_REVIEW,
    PRIOR_FINDING_VERDICTS,
    PRIOR_VERDICT_EVIDENCE_MAX,
    PRIOR_VERDICTS_REQUIRING_EVIDENCE,
    compact_text,
    local_reference_number,
)
from .postgres.publications import (
    PreparationFinding,
    PreviousPublicationFinding,
    PublicationPreparationContext,
)
from .publication_partition import split_publication_body
from .review_renderer import (
    ClosedFinding,
    PublishedFinding,
    ReviewBlock,
    ReviewCoverageSummary,
    UncheckedFinding,
    render_review,
    review_blocks_to_json,
    review_markdown_from_blocks,
)


class PublicationPlanningError(ValueError):
    """Submitted verdicts conflict with the frozen review facts."""


@dataclass(frozen=True, slots=True)
class PlannedPublication:
    plan: PublicationPlan
    findings_count: int
    suggestions_count: int
    resolved_count: int
    ignored_previous_verdicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PriorVerdict:
    verdict: str
    evidence: str


def _previous_verdicts(
    raw: object,
    *,
    current_references: frozenset[str],
    ignored_references: frozenset[str],
) -> tuple[dict[str, _PriorVerdict], tuple[str, ...]]:
    if raw is None:
        return {}, ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise PublicationPlanningError("previous_verdicts must be an array")
    values = cast(Sequence[object], raw)
    if len(values) > MAX_FINDINGS_PER_REVIEW:
        raise PublicationPlanningError("previous_verdicts contains too many items")
    normalized: dict[str, _PriorVerdict] = {}
    ignored: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise PublicationPlanningError(
                f"previous_verdicts[{index}] must be an object"
            )
        item = cast(Mapping[object, object], value)
        reference = str(item.get("local_reference") or "").strip().upper()
        try:
            reference_number = local_reference_number(reference)
        except ValueError as exc:
            raise PublicationPlanningError(
                f"previous_verdicts[{index}].local_reference must be F1, F2, ..."
            ) from exc
        if reference_number < 1:
            raise PublicationPlanningError(
                f"previous_verdicts[{index}].local_reference must be F1, F2, ..."
            )
        if reference in ignored_references:
            continue
        if reference not in current_references:
            if reference not in ignored:
                ignored.append(reference)
            continue
        if reference in normalized:
            raise PublicationPlanningError(
                f"duplicate previous verdict for {reference}"
            )
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in PRIOR_FINDING_VERDICTS:
            raise PublicationPlanningError(
                f"previous_verdicts[{index}].verdict is not supported"
            )
        evidence = " ".join(str(item.get("evidence") or "").strip().split())
        if len(evidence) > PRIOR_VERDICT_EVIDENCE_MAX:
            raise PublicationPlanningError(
                f"previous_verdicts[{index}].evidence is too long"
            )
        if verdict in PRIOR_VERDICTS_REQUIRING_EVIDENCE and not evidence:
            raise PublicationPlanningError(
                f"previous_verdicts[{index}].evidence is required"
            )
        normalized[reference] = _PriorVerdict(verdict=verdict, evidence=evidence)
    return normalized, tuple(ignored)


def _published_finding(item: PreparationFinding) -> PublishedFinding:
    return {
        "local_reference": item.local_reference,
        "fingerprint": item.fingerprint,
        "observation_id": item.occurrence_id,
        "context_hash": item.context_hash,
        "rule_id": item.rule_id,
        "category": item.category,
        "path": item.path,
        "line": item.line,
        "title": item.title,
        "severity": item.severity,
        "publication_score": item.publication_score,
        "evidence": item.evidence,
        "disproof_checks": item.disproof_checks,
        "impact": item.impact,
        "smallest_fix": item.smallest_fix,
        # Suggestions are delivered as a separate immutable GitHub review. The
        # summary never promises that external side effect before it succeeds.
        "suggestion_available": False,
    }


def _closed(
    item: PreviousPublicationFinding,
    *,
    verdict: Literal["resolved", "invalidated", "suppressed"],
    evidence: str,
) -> ClosedFinding:
    return {
        "local_reference": item.local_reference,
        "fingerprint": item.fingerprint,
        "observation_id": item.occurrence_id,
        "context_hash": item.context_hash,
        "verdict": verdict,
        "title": item.title,
        "evidence": evidence,
    }


def _coverage(context: PublicationPreparationContext) -> ReviewCoverageSummary:
    item = context.coverage
    changed_paths = item.changed_files_registered
    diff_exposed = item.changed_paths_with_complete_diff
    unavailable = len(item.unavailable_paths)
    truncated = len(item.truncated_paths)
    material = json.dumps(
        {
            "state": item.state,
            "reported": item.changed_files_reported,
            "registered": changed_paths,
            "complete_diff": diff_exposed,
            "source_reads": item.changed_paths_with_source_reads,
            "supporting_reads": item.supporting_context_paths_read,
            "ranges": item.context_ranges_read,
            "unavailable": item.unavailable_paths,
            "truncated": item.truncated_paths,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    states: dict[str, Literal["complete", "incomplete", "unknown"]] = {
        "complete": "complete",
        "incomplete": "incomplete",
        "unknown": "unknown",
    }
    try:
        state = states[item.state]
    except KeyError as exc:
        raise PublicationPlanningError(
            f"stored coverage state is unsupported: {item.state}"
        ) from exc
    return {
        "state": state,
        "changed_paths": changed_paths,
        "diff_exposed": diff_exposed,
        "context_paths_read": (
            item.changed_paths_with_source_reads + item.supporting_context_paths_read
        ),
        "context_ranges_read": item.context_ranges_read,
        "changed_paths_with_diff": diff_exposed,
        "changed_paths_with_source_reads": item.changed_paths_with_source_reads,
        "supporting_context_paths_read": item.supporting_context_paths_read,
        "changed_files_reported": item.changed_files_reported,
        "changed_files_registered": changed_paths,
        "changed_file_registration_complete": item.registration_complete,
        "unavailable": unavailable,
        "diff_truncated": truncated,
        "coverage_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "unavailable_paths": list(item.unavailable_paths),
        "truncated_paths": list(item.truncated_paths),
    }


def _publication_key(
    context: PublicationPreparationContext, markdown: str
) -> str:
    material = "\n".join(
        (
            context.repository,
            str(context.pr_number),
            context.base_sha,
            context.head_sha,
            context.policy_revision,
            str(context.run_id),
            hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _suggestion_part(
    findings: Sequence[PreparationFinding], publication_key: str
) -> PublicationPartInput | None:
    comments: list[JsonValue] = []
    marker = f"<!-- review-agent:canonical publication={publication_key} -->"
    for item in findings:
        suggestion = item.suggestion
        if suggestion is None:
            continue
        multiline = suggestion.start_line != suggestion.end_line
        comments.append(
            {
                "path": suggestion.path,
                "body": (
                    f"**{item.local_reference} · Optional atomic patch**\n\n"
                    "```suggestion\n"
                    f"{suggestion.replacement_text.rstrip()}\n"
                    "```\n\n"
                    f"{marker}"
                ),
                "line": suggestion.end_line,
                "side": "RIGHT",
                "start_line": suggestion.start_line if multiline else None,
                "start_side": "RIGHT" if multiline else None,
            }
        )
    if not comments:
        return None
    return PublicationPartInput(
        part_type=PublicationPartType.SUGGESTION_REVIEW,
        part_number=1,
        payload_schema_version=1,
        payload={
            "body": "Optional atomic patches from this review.",
            "comments": comments,
        },
    )


def build_publication(
    context: PublicationPreparationContext,
    *,
    previous_verdicts: object,
    feedback_enabled: bool,
    max_comment_bytes: int,
) -> PlannedPublication:
    """Resolve prior state, render bytes, and build the provider payloads."""
    dropped_reasons = dict(context.dropped_reasons)
    admitted = tuple(
        item
        for item in context.current
        if not item.suppressed and item.occurrence_id not in context.dropped_occurrence_ids
    )
    current_by_fingerprint = {item.fingerprint: item for item in admitted}
    previous_by_fingerprint = {item.fingerprint: item for item in context.previous}
    ignored_refs = frozenset(
        previous_by_fingerprint[item.fingerprint].local_reference
        for item in context.current
        if item.occurrence_id in context.dropped_occurrence_ids
        and item.fingerprint in previous_by_fingerprint
    )
    verdicts, ignored = _previous_verdicts(
        previous_verdicts,
        current_references=frozenset(
            item.local_reference for item in context.previous
        ),
        ignored_references=ignored_refs,
    )

    closed: list[ClosedFinding] = []
    unchecked: list[UncheckedFinding] = []
    still_present: list[str] = []
    partially_resolved: list[str] = []
    publication_findings: list[PublicationFindingInput] = []
    for item in admitted:
        publication_findings.append(
            PublicationFindingInput(
                finding_id=item.finding_id,
                source_finding_occurrence_id=item.occurrence_id,
                source_review_run_id=item.source_run_id,
                local_reference=item.local_reference,
                outcome=PublicationFindingOutcome.CURRENT,
            )
        )

    for previous in context.previous:
        current = current_by_fingerprint.get(previous.fingerprint)
        matching_current = next(
            (
                item
                for item in context.current
                if item.fingerprint == previous.fingerprint
            ),
            None,
        )
        supplied = verdicts.get(previous.local_reference)
        if current is not None:
            if supplied is None or supplied.verdict == "still_present":
                still_present.append(previous.local_reference)
                continue
            if supplied.verdict == "partially_resolved":
                partially_resolved.append(previous.local_reference)
                continue
            raise PublicationPlanningError(
                f"previous verdict {previous.local_reference}={supplied.verdict} "
                "conflicts with a newly recorded finding"
            )

        if matching_current is not None and matching_current.suppressed:
            evidence = "A current human suppression matches this file version."
            closed.append(_closed(previous, verdict="suppressed", evidence=evidence))
            outcome = PublicationFindingOutcome.SUPPRESSED
        elif (
            matching_current is not None
            and matching_current.occurrence_id in context.dropped_occurrence_ids
        ):
            evidence = compact_text(
                dropped_reasons.get(matching_current.occurrence_id) or "Verifier dropped the candidate.",
                maximum=PRIOR_VERDICT_EVIDENCE_MAX,
            )
            closed.append(_closed(previous, verdict="invalidated", evidence=evidence))
            outcome = PublicationFindingOutcome.INVALIDATED
        elif previous.suppressed:
            evidence = "A current human suppression matches this file version."
            closed.append(_closed(previous, verdict="suppressed", evidence=evidence))
            outcome = PublicationFindingOutcome.SUPPRESSED
        elif supplied is None or supplied.verdict == "not_checked":
            evidence = "Not rechecked in this review."
            unchecked.append(
                {
                    "local_reference": previous.local_reference,
                    "fingerprint": previous.fingerprint,
                    "title": previous.title,
                }
            )
            outcome = PublicationFindingOutcome.NOT_CHECKED
        elif supplied.verdict in {"resolved", "invalidated"}:
            literal = cast(Literal["resolved", "invalidated"], supplied.verdict)
            evidence = supplied.evidence
            closed.append(_closed(previous, verdict=literal, evidence=evidence))
            outcome = (
                PublicationFindingOutcome.RESOLVED
                if supplied.verdict == "resolved"
                else PublicationFindingOutcome.INVALIDATED
            )
        elif supplied.verdict == "suppressed":
            raise PublicationPlanningError(
                f"previous verdict {previous.local_reference}=suppressed has no active human suppression"
            )
        else:
            raise PublicationPlanningError(
                f"previous verdict {previous.local_reference}={supplied.verdict} "
                "must also record the still-current finding"
            )
        publication_findings.append(
            PublicationFindingInput(
                finding_id=previous.finding_id,
                source_finding_occurrence_id=previous.occurrence_id,
                source_review_run_id=previous.source_run_id,
                local_reference=previous.local_reference,
                outcome=outcome,
                outcome_evidence=evidence,
            )
        )

    pending = len(admitted) + len(unchecked)
    if pending > MAX_FINDINGS_PER_REVIEW:
        raise PublicationPlanningError(
            f"review would leave {pending} pending findings; close prior findings before publishing"
        )
    findings = tuple(_published_finding(item) for item in admitted)
    new_refs = tuple(
        item.local_reference
        for item in admitted
        if context.previous_review_number is not None
        and item.fingerprint not in context.published_fingerprints
    )
    returned_refs = tuple(
        item.local_reference
        for item in admitted
        if context.previous_review_number is not None
        and item.fingerprint in context.published_fingerprints
        and item.fingerprint not in previous_by_fingerprint
    )
    rendered = render_review(
        repository=context.repository,
        pr_number=context.pr_number,
        head_sha=context.head_sha,
        findings=findings,
        closed=closed,
        unchecked=unchecked,
        still_present=still_present,
        partially_resolved=partially_resolved,
        new_refs=new_refs,
        returned_refs=returned_refs,
        not_checked_refs=tuple(item["local_reference"] for item in unchecked),
        feedback_enabled=feedback_enabled,
        coverage=_coverage(context),
        review_number=context.review_number,
        previous_review_number=context.previous_review_number,
        previous_head_sha=context.previous_head_sha,
    )
    key = _publication_key(context, rendered.markdown)
    marker = ReviewBlock(
        kind="metadata",
        markdown=f"<!-- review-agent:canonical publication={key} -->",
    )
    blocks = (*rendered.blocks, marker)
    markdown = review_markdown_from_blocks(blocks)
    blocks_json = review_blocks_to_json(blocks)
    issue_parts = split_publication_body(
        markdown,
        publication_key=key,
        max_comment_bytes=max_comment_bytes,
        rendered_blocks_json=blocks_json,
    )
    parts: list[PublicationPartInput] = [
        PublicationPartInput(
            part_type=(
                PublicationPartType.SUMMARY
                if part.part_number == 1
                else PublicationPartType.CONTINUATION
            ),
            part_number=part.part_number,
            payload_schema_version=1,
            payload={"body": part.body},
        )
        for part in issue_parts
    ]
    suggestion_part = _suggestion_part(admitted, key)
    if suggestion_part is not None:
        parts.append(suggestion_part)
    plan = resolve_publication_plan(
        publication_key=key,
        rendered_markdown=markdown,
        rendered_blocks_schema_version=1,
        rendered_blocks=tuple(
            {"kind": block.kind, "markdown": block.markdown} for block in blocks
        ),
        parts=parts,
        findings=publication_findings,
    )
    return PlannedPublication(
        plan=plan,
        findings_count=len(admitted),
        suggestions_count=sum(item.suggestion is not None for item in admitted),
        resolved_count=sum(item["verdict"] == "resolved" for item in closed),
        ignored_previous_verdicts=ignored,
    )
