"""Pure construction of one immutable PostgreSQL publication plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from .domain.publication import (
    CHECK_OUTPUT_MAX_BYTES,
    DOCUMENTATION_CHECK_NAME,
    JsonValue,
    publication_marker,
    PublicationDomainError,
    PublicationFindingInput,
    PublicationFindingOutcome,
    PublicationPartInput,
    PublicationPartType,
    PublicationPlan,
    resolve_publication_plan,
)
from .domain.documentation_review import DocumentationResult
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
from .publication_partition import publication_content_budget, split_publication_body
from .review_renderer import (
    ClosedFinding,
    PublishedFinding,
    ReviewBlock,
    ReviewCoverageSummary,
    RepositoryDecisionSummary,
    UncheckedFinding,
    render_review,
    review_heading,
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
    item: PreviousPublicationFinding | PreparationFinding,
    *,
    verdict: Literal["resolved", "invalidated", "suppressed", "reconciled"],
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
    material = json.dumps(
        {
            "state": item.state,
            "reported": item.changed_files_reported,
            "registered": changed_paths,
            "complete_diff": diff_exposed,
            "source_reads": item.changed_paths_with_source_reads,
            "supporting_reads": item.supporting_context_paths_read,
            "ranges": item.context_ranges_read,
            "unavailable": item.unavailable_count,
            "truncated": item.truncated_count,
            "unseen": item.unseen_count,
            "examples": [asdict(example) for example in item.examples],
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
        "unavailable": item.unavailable_count,
        "diff_truncated": item.truncated_count,
        "diff_unseen": item.unseen_count,
        "coverage_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "examples": item.examples,
    }


def _repository_decisions(
    context: PublicationPreparationContext,
) -> RepositoryDecisionSummary:
    snapshot = context.repository_decisions
    return {
        "status": snapshot.status,
        "failure_code": (
            "decision_snapshot_missing"
            if snapshot.status == "pending"
            else snapshot.failure_code
        ),
        "base_sha": snapshot.base_sha,
        "snapshot_hash": snapshot.snapshot_hash,
        "decision_ids": [decision.id for decision in snapshot.decisions],
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
    reconciliations = {int(item.finding_id): item for item in context.reconciliations}
    current_ids = {item.finding_id for item in context.current
                   if item.occurrence_id not in context.dropped_occurrence_ids}
    for item in context.current:
        reconciliation = reconciliations.get(item.finding_id)
        if reconciliation is not None and reconciliation.canonical_finding_id not in current_ids:
            raise PublicationPlanningError(
                f"record the rechecked canonical finding {reconciliation.canonical_reference} "
                "with its existing stable identity before publishing a known duplicate"
            )
    admitted = tuple(
        item
        for item in context.current
        if not item.suppressed and item.occurrence_id not in context.dropped_occurrence_ids
        and item.finding_id not in reconciliations
    )
    claims: dict[tuple[str, str, str, str], str] = {}
    for item in admitted:
        claim = (item.path, item.evidence, item.impact, item.smallest_fix)
        prior_reference = claims.get(claim)
        if prior_reference is not None:
            raise PublicationPlanningError(
                f"{prior_reference} and {item.local_reference} state the same claim; "
                "reconcile their root cause before publishing; if they are independent, "
                "a new review must record evidence distinguishing them"
            )
        claims[claim] = item.local_reference
    current_by_fingerprint = {item.fingerprint: item for item in admitted}
    previous_by_fingerprint = {item.fingerprint: item for item in context.previous}
    ignored_refs = frozenset(
        previous_by_fingerprint[item.fingerprint].local_reference
        for item in context.current
        if item.occurrence_id in context.dropped_occurrence_ids
        and item.fingerprint in previous_by_fingerprint
    ) | frozenset(item.local_reference for item in context.previous if item.finding_id in reconciliations)
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
    reconciled_items = {
        item.finding_id: item for item in (*context.previous, *context.current)
        if item.finding_id in reconciliations
    }
    for item in reconciled_items.values():
        reconciliation = reconciliations[item.finding_id]
        evidence = compact_text(
            f"Same root cause as {reconciliation.canonical_reference}. {reconciliation.evidence}",
            maximum=PRIOR_VERDICT_EVIDENCE_MAX,
        )
        closed.append(_closed(item, verdict="reconciled", evidence=evidence))
        publication_findings.append(PublicationFindingInput(
            finding_id=item.finding_id, source_finding_occurrence_id=item.occurrence_id,
            source_review_run_id=item.source_run_id, local_reference=item.local_reference,
            outcome=PublicationFindingOutcome.RECONCILED, outcome_evidence=evidence,
        ))
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
        if previous.finding_id in reconciliations:
            continue
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
        repository_decisions=_repository_decisions(context),
        review_number=context.review_number,
        previous_review_number=context.previous_review_number,
        previous_head_sha=context.previous_head_sha,
        # Packing appends one newline to each rendered block.
        max_header_bytes=(
            publication_content_budget(
                review_heading(context.review_number),
                max_comment_bytes=max_comment_bytes,
            )
            - 1
        ),
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


def build_documentation_publication(
    context: PublicationPreparationContext,
    result: "DocumentationResult",
    *,
    report_blocks: Sequence[ReviewBlock] = (),
    max_comment_bytes: int,
) -> PlannedPublication:
    """Freeze the complete docs report and its deterministic check/overflow plan."""
    from .domain.documentation_review import DocumentationOutcome
    if not result.frozen or result.outcome is None:
        raise PublicationPlanningError("documentation result must be finalized before publication")
    if (int(result.run_id), result.base_sha, result.head_sha) != (context.run_id, context.base_sha, context.head_sha):
        raise PublicationPlanningError("documentation result does not match its publication subject")
    current = tuple(item for item in context.current if not item.suppressed and item.occurrence_id not in context.dropped_occurrence_ids)
    if current and result.outcome is not DocumentationOutcome.FINDINGS:
        raise PublicationPlanningError("retained documentation findings require the findings outcome")
    labels = {
        DocumentationOutcome.NOT_NEEDED: "No documentation review needed",
        DocumentationOutcome.NO_MISMATCH_FOUND: "No documentation mismatch found",
        DocumentationOutcome.FINDINGS: "Documentation changes recommended",
        DocumentationOutcome.INCOMPLETE: "Documentation review incomplete",
        DocumentationOutcome.NOT_CONFIGURED: "Documentation rules not configured",
        DocumentationOutcome.INVALID_CONFIGURATION: "Documentation configuration needs attention",
        DocumentationOutcome.UNAVAILABLE: "Documentation review unavailable",
    }
    title = labels[result.outcome]
    conclusion = "skipped" if result.outcome is DocumentationOutcome.NOT_NEEDED else (
        "success" if result.outcome is DocumentationOutcome.NO_MISMATCH_FOUND and result.coverage_complete else "neutral"
    )
    scope = result.scope
    details = (
        f"## Documentation review\n\n**{title}**\n\n"
        f"Reviewed commit: `{result.head_sha}`\n\n"
        f"Coverage: {'complete for the selected scope' if result.coverage_complete else 'incomplete'}. "
        f"{len(scope.areas) if scope else 0} selected areas; "
        f"{len(scope.documents) if scope else 0} documentation paths. "
        f"Semantic assessment: {'used' if result.semantic_inference_used else 'not used'}."
    )
    if result.incomplete_reasons:
        details += "\n\nLimitations:\n" + "\n".join(f"- {reason}" for reason in result.incomplete_reasons)
    blocks = [ReviewBlock(kind="header", markdown=details), *report_blocks]
    # Findings always render from persisted records; caller prose cannot omit one.
    for item in current:
        blocks.append(ReviewBlock(kind="finding", markdown=(
            f"### {item.local_reference} · {item.title}\n\n"
            f"`{item.path}:{item.line}`\n\n{item.evidence}\n\n"
            f"**Impact:** {item.impact}\n\n**Suggested correction:** {item.smallest_fix}"
        )))
    publication_findings = [PublicationFindingInput(
        finding_id=item.finding_id, source_finding_occurrence_id=item.occurrence_id,
        source_review_run_id=context.run_id, local_reference=item.local_reference,
        outcome=PublicationFindingOutcome.CURRENT,
    ) for item in current]
    prior_verdicts = {item.local_reference: item for item in result.assessment.previous} if result.assessment else {}
    current_fingerprints = {item.fingerprint for item in current}
    resolved_count = 0
    for previous in context.previous:
        if previous.fingerprint in current_fingerprints:
            continue
        supplied = prior_verdicts.get(previous.local_reference)
        if supplied is not None and (supplied.fingerprint != previous.fingerprint or supplied.occurrence_id != previous.occurrence_id):
            raise PublicationPlanningError("documentation prior verdict no longer matches the published occurrence")
        resolved = not previous.suppressed and supplied is not None and supplied.verdict == "resolved"
        outcome = (PublicationFindingOutcome.SUPPRESSED if previous.suppressed else
                   PublicationFindingOutcome.RESOLVED if resolved else PublicationFindingOutcome.NOT_CHECKED)
        explanation = ("A current human suppression matches this evidence version." if previous.suppressed else
                       supplied.rationale if supplied else "Not rechecked in this documentation review.")
        label = "Suppressed" if previous.suppressed else "Resolved" if resolved else "Not checked"
        if resolved:
            resolved_count += 1
        blocks.append(ReviewBlock(kind="closed_history" if resolved or previous.suppressed else "unchecked_history", markdown=(
            f"### {previous.local_reference} · {label} · {previous.title}\n\n{explanation}"
        )))
        publication_findings.append(PublicationFindingInput(
            finding_id=previous.finding_id, source_finding_occurrence_id=previous.occurrence_id,
            source_review_run_id=previous.source_run_id, local_reference=previous.local_reference,
            outcome=outcome, outcome_evidence=explanation,
        ))
    blocks.append(ReviewBlock(kind="feedback_help", markdown=(
        "Post `/review docs` as a new top-level PR comment after updating the documents. "
        "To correct a finding, use `/review docs false-positive F1 <reason>`. "
        "Report scope problems with `/review docs feedback scope <reason>` or missed gaps "
        "with `/review docs feedback missed <reason>`. Replace F1 with the finding reference."
    )))
    markdown = review_markdown_from_blocks(blocks)
    key = _publication_key(context, markdown)
    blocks.append(ReviewBlock(kind="metadata", markdown=f"<!-- {publication_marker(key)} -->"))
    markdown = review_markdown_from_blocks(blocks)
    parts: list[PublicationPartInput] = []
    summary = markdown
    report_numbers: list[JsonValue] = []
    if len(markdown.encode("utf-8")) > CHECK_OUTPUT_MAX_BYTES:
        try:
            overflow = split_publication_body(
                markdown, publication_key=key, max_comment_bytes=max_comment_bytes,
                rendered_blocks_json=review_blocks_to_json(blocks),
            )
        except PublicationDomainError as exc:
            if exc.code != "body_too_large":
                raise
            overflow = []
            conclusion = "neutral"
        for part in overflow:
            parts.append(PublicationPartInput(
                part_type=PublicationPartType.SUMMARY if part.part_number == 1 else PublicationPartType.CONTINUATION,
                part_number=part.part_number, payload_schema_version=1, payload={"body": part.body},
            ))
            report_numbers.append(part.part_number)
        summary = (
            f"## Documentation review\n\n**{title}**\n\nReviewed commit: `{result.head_sha}`. "
            f"Coverage: {'complete for the selected scope' if result.coverage_complete else 'incomplete'}.\n\n"
        )
        if overflow:
            summary += "The complete report is available in the linked report parts."
        else:
            summary += (
                "Delivery limitation: a report block exceeds GitHub's comment limit. "
                "The complete report and findings are retained in review history; "
                "this check does not claim that the complete report was delivered to GitHub."
            )
        if len(summary.encode("utf-8")) + len(report_numbers) * 300 > CHECK_OUTPUT_MAX_BYTES:
            raise PublicationPlanningError("documentation overflow index exceeds the check output limit")
    parts.append(PublicationPartInput(
        part_type=PublicationPartType.CHECK_RUN, part_number=1, payload_schema_version=1,
        payload={"name": DOCUMENTATION_CHECK_NAME, "head_sha": result.head_sha,
                 "external_id": publication_marker(key), "status": "completed",
                 "conclusion": conclusion, "title": title, "summary": summary,
                 "report_part_numbers": report_numbers},
    ))
    plan = resolve_publication_plan(
        publication_key=key, rendered_markdown=markdown, rendered_blocks_schema_version=1,
        rendered_blocks=tuple({"kind": b.kind, "markdown": b.markdown} for b in blocks),
        parts=parts,
        findings=tuple(publication_findings),
    )
    return PlannedPublication(plan, len(current), 0, resolved_count, ())
