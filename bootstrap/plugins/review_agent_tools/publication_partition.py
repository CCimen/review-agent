"""Pure deterministic partitioning for review publication payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from .domain.publication import PublicationDomainError, publication_marker
from .review_identity import CONTINUATION_LEAD, REVIEW_COMMENT_TITLE
from .review_renderer import (
    ReviewBlock,
    review_blocks_from_json,
    review_markdown_from_blocks,
)

_HISTORICAL_TRUNCATION_NOTICE = (
    "_Historical details were shortened to fit GitHub comment limits; "
    "the full review text remains in review memory._\\n\\n"
)


@dataclass(frozen=True)
class PublicationPart:
    part_number: int
    body: str


class HistoricalPublication(TypedDict):
    review_number: int | None
    repository: str
    pr_number: int
    head_sha: str
    publication_key: str
    rendered_markdown: str
    rendered_blocks_json: str
    current_findings_count: int
    superseded_by_review_number: int | None
    superseded_by_comment_id: int


def _part_marker(publication_key: str, part_number: int, total_parts: int) -> str:
    return (
        f"{publication_marker(publication_key)} "
        f"part={part_number}/{total_parts}"
    )


def publication_body_size(body: str) -> int:
    return len(body.encode("utf-8"))


def _pack_blocks(blocks: list[str], max_bytes: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if publication_body_size(block) > max_bytes:
            raise PublicationDomainError("body_too_large")
        candidate = current + block
        if current and publication_body_size(candidate) > max_bytes:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _publication_blocks(
    body: str, *, rendered_blocks_json: str, publication_key: str
) -> list[str]:
    marker = f"<!-- {publication_marker(publication_key)} -->"
    if rendered_blocks_json:
        try:
            blocks = review_blocks_from_json(
                rendered_blocks_json, fallback_markdown=body
            )
        except ValueError as exc:
            raise PublicationDomainError("rendered_blocks_invalid") from exc
        if review_markdown_from_blocks(blocks) != body:
            raise PublicationDomainError("rendered_blocks_mismatch")
    else:
        blocks = (ReviewBlock(kind="header", markdown=body),)

    content_blocks: list[str] = []
    for block in blocks:
        markdown = block.markdown.replace(marker, "").strip()
        if markdown:
            content_blocks.append(markdown + "\n")
    return content_blocks


def _publication_heading(body: str) -> str:
    default = f"## {REVIEW_COMMENT_TITLE}"
    first_line = body.splitlines()[0].strip() if body.strip() else ""
    return first_line if first_line.startswith(default) else default


def _part_heading(heading: str, part_number: int, total_parts: int) -> str:
    return f"{heading} · Part {part_number} of {total_parts}"


def _continuation_prefix(heading: str, part_number: int, total_parts: int) -> str:
    if part_number == 1:
        return ""
    return (
        f"{_part_heading(heading, part_number, total_parts)}\n\n{CONTINUATION_LEAD}\n\n"
    )


def _with_part_heading(
    body: str, heading: str, part_number: int, total_parts: int
) -> str:
    if part_number != 1:
        return _continuation_prefix(heading, part_number, total_parts) + body
    replacement = _part_heading(heading, part_number, total_parts)
    return replacement + body[len(heading) :] if body.startswith(heading) else body


def split_publication_body(
    body: str,
    *,
    publication_key: str,
    max_comment_bytes: int,
    rendered_blocks_json: str = "",
) -> list[PublicationPart]:
    single_body = (
        f"{body.rstrip()}\n\n"
        f"<!-- {_part_marker(publication_key, 1, 1)} -->\n"
    )
    if publication_body_size(single_body) <= max_comment_bytes:
        return [PublicationPart(part_number=1, body=single_body)]

    heading = _publication_heading(body)
    reserved = publication_body_size(
        _continuation_prefix(heading, 9999, 9999)
        + "\n\n<!-- "
        + _part_marker(publication_key, 9999, 9999)
        + " -->\n"
    )
    content_budget = max_comment_bytes - reserved
    if content_budget < 200:
        raise PublicationDomainError("body_too_large")

    blocks = _publication_blocks(
        body,
        rendered_blocks_json=rendered_blocks_json,
        publication_key=publication_key,
    )
    chunks = _pack_blocks(blocks, content_budget)
    total_parts = len(chunks)
    parts: list[PublicationPart] = []
    for index, chunk in enumerate(chunks, start=1):
        part_body = _with_part_heading(chunk.rstrip(), heading, index, total_parts)
        part_body = (
            f"{part_body.rstrip()}\n\n"
            f"<!-- {_part_marker(publication_key, index, total_parts)} -->\n"
        )
        if publication_body_size(part_body) > max_comment_bytes:
            raise PublicationDomainError("body_too_large")
        parts.append(PublicationPart(part_number=index, body=part_body))
    return parts


def _comment_url(repository: str, pr_number: int, comment_id: int) -> str:
    return f"https://github.com/{repository}/pull/{pr_number}#issuecomment-{comment_id}"


def _historical_content_blocks(
    publication: HistoricalPublication,
) -> list[str]:
    marker = f"<!-- {publication_marker(publication['publication_key'])} -->"
    blocks_json = publication["rendered_blocks_json"]
    if blocks_json:
        blocks = review_blocks_from_json(
            blocks_json, fallback_markdown=publication["rendered_markdown"]
        )
        if review_markdown_from_blocks(blocks) != publication["rendered_markdown"]:
            raise PublicationDomainError("rendered_blocks_mismatch")
        content = [
            block.markdown.replace(marker, "").strip()
            for block in blocks
            if block.kind
            not in {"suggestion_help", "fix_brief", "feedback_help", "metadata"}
        ]
    else:
        content = [publication["rendered_markdown"].replace(marker, "").strip()]
    return [f"{block}\n\n" for block in content if block]


def _historical_label(review_number: int | None) -> str:
    return f"Review {review_number}" if review_number is not None else "Previous review"


def _truncate_block_to_budget(block: str, max_bytes: int) -> tuple[str, bool]:
    if publication_body_size(block) <= max_bytes:
        return block, False
    suffix = "\n\n[truncated]\n\n"
    available = max_bytes - publication_body_size(suffix)
    if available < 100:
        return "", True
    encoded = block.encode("utf-8")[:available]
    return encoded.decode("utf-8", errors="ignore").rstrip() + suffix, True


def _fit_historical_chunks(
    content_blocks: list[str], *, content_budget: int, max_parts: int
) -> list[str]:
    if max_parts < 1:
        raise PublicationDomainError("superseded_comment_missing")
    if publication_body_size(_HISTORICAL_TRUNCATION_NOTICE) > content_budget:
        raise PublicationDomainError("superseded_body_too_large")

    retained = list(content_blocks)
    truncated = False
    while retained:
        candidate: list[str] = []
        block_truncated = False
        for block in retained:
            clipped, was_truncated = _truncate_block_to_budget(block, content_budget)
            if clipped:
                candidate.append(clipped)
            block_truncated = block_truncated or was_truncated
        if truncated or block_truncated:
            candidate.append(_HISTORICAL_TRUNCATION_NOTICE)
        try:
            chunks = _pack_blocks(candidate, content_budget)
        except PublicationDomainError:
            chunks = []
        if chunks and len(chunks) <= max_parts:
            return chunks
        retained.pop()
        truncated = True

    return [_HISTORICAL_TRUNCATION_NOTICE]


def historical_bodies(
    publication: HistoricalPublication,
    *,
    max_comment_bytes: int,
    target_parts: int,
) -> list[PublicationPart]:
    old_label = _historical_label(publication["review_number"])
    new_label = _historical_label(publication["superseded_by_review_number"])
    new_url = _comment_url(
        publication["repository"],
        publication["pr_number"],
        publication["superseded_by_comment_id"],
    )
    summary = (
        f"{old_label} at `{publication['head_sha'][:8]}` · "
        f"{publication['current_findings_count']} findings"
    )
    content_blocks = _historical_content_blocks(publication)
    if not content_blocks:
        raise PublicationDomainError("superseded_body_empty")

    reserved_template = (
        f"## {REVIEW_COMMENT_TITLE} · {old_label} · Superseded - 999 of 999\n\n"
        f"> [!NOTE]\n"
        f"> **Superseded by [{new_label}]({new_url}).**\n"
        f"> This review describes commit `{publication['head_sha'][:8]}` and is "
        "retained as historical context.\n\n"
        "<details>\n"
        f"<summary>{summary}</summary>\n\n"
        "</details>\n\n"
        f"<!-- {_part_marker(publication['publication_key'], 999, 999)} -->\n"
    )
    content_budget = max_comment_bytes - publication_body_size(reserved_template)
    if content_budget < 200:
        raise PublicationDomainError("superseded_body_too_large")
    chunks = _fit_historical_chunks(
        content_blocks, content_budget=content_budget, max_parts=target_parts
    )
    parts: list[PublicationPart] = []
    for part_number, chunk in enumerate(chunks, start=1):
        heading = f"## {REVIEW_COMMENT_TITLE} · {old_label} · Superseded"
        if target_parts > 1:
            heading = f"{heading} - {part_number} of {target_parts}"
        body = (
            f"{heading}\n\n"
            f"> [!NOTE]\n"
            f"> **Superseded by [{new_label}]({new_url}).**\n"
            f"> This review describes commit `{publication['head_sha'][:8]}` and is "
            "retained as historical context.\n\n"
            "<details>\n"
            f"<summary>{summary}</summary>\n\n"
            f"{chunk.rstrip()}\n\n"
            "</details>\n\n"
            f"<!-- {_part_marker(publication['publication_key'], part_number, target_parts)} -->\n"
        )
        if publication_body_size(body) > max_comment_bytes:
            raise PublicationDomainError("superseded_body_too_large")
        parts.append(PublicationPart(part_number=part_number, body=body))
    return parts


def extra_superseded_body(
    publication: HistoricalPublication,
    *,
    part_number: int,
    total_parts: int,
) -> str:
    old_label = _historical_label(publication["review_number"])
    new_label = _historical_label(publication["superseded_by_review_number"])
    new_url = _comment_url(
        publication["repository"],
        publication["pr_number"],
        publication["superseded_by_comment_id"],
    )
    return (
        f"## {REVIEW_COMMENT_TITLE} · {old_label} · Superseded\n\n"
        f"> [!NOTE]\n"
        f"> **Superseded by [{new_label}]({new_url}).**\n"
        "> This continuation comment is retained only to preserve the historical "
        "PR timeline.\n\n"
        f"<!-- {_part_marker(publication['publication_key'], part_number, total_parts)} -->\n"
    )


