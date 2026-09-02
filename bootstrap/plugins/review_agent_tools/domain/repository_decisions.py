"""Typed repository design decisions used as review evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import re
import tomllib
from typing import cast


# These fixed ceilings bound optional ADR metadata, never PR discovery or source
# review. Ten maximally sized typed headers stay well below the 160 KiB begin-tool
# result budget; the index guards bound parse and path-match work for a 3,000-file
# GitHub PR. Exceeding a guard disables only ADR evidence for that run.
MAX_INDEX_ENTRIES = 200
MAX_MATCHED_DECISIONS = 10
MAX_FRONTMATTER_LINES = 60
MAX_INDEX_BYTES = 64 * 1024
MAX_ADR_HEADER_BYTES = 32 * 1024
MAX_GLOBS_PER_DECISION = 20
MAX_TOTAL_GLOBS = 1_000
MAX_ON_CHANGE_ITEMS = 10
# One version line plus a canonical multiline table for every allowed pattern.
# The byte ceiling remains authoritative; this line ceiling bounds gateway work
# without rejecting a maximally populated, normally formatted index.
MAX_INDEX_LINES = 1 + MAX_INDEX_ENTRIES * (MAX_GLOBS_PER_DECISION + 6)
_MAX_PATH_CHARS = 500
_MAX_INVARIANT_CHARS = 500
_MAX_ON_CHANGE_CHARS = 300
_DECISION_ID_RE = re.compile(r"^ADR-[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_INVARIANT_LINE_RE = re.compile(r"^\s*(?:invariant|['\"]invariant['\"])\s*=")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RepositoryDecisionError(ValueError):
    """Repository decision content does not match the trusted data contract."""


@dataclass(frozen=True, slots=True)
class DecisionIndexEntry:
    id: str
    adr_path: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DecisionIndex:
    entries: tuple[DecisionIndexEntry, ...]


@dataclass(frozen=True, slots=True)
class DecisionIndexMatch:
    entry: DecisionIndexEntry
    matched_path_count: int


@dataclass(frozen=True, slots=True)
class RepositoryDecision:
    id: str
    adr_path: str
    applies_to: tuple[str, ...]
    title: str
    status: str
    invariant: str
    on_change: tuple[str, ...]
    evidence: str | None
    origin_pr: int | None
    supersedes: str | None
    invariant_line: int
    matched_path_count: int
    metadata_hash: str


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise RepositoryDecisionError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or "\x00" in normalized or len(normalized) > maximum:
        raise RepositoryDecisionError(
            f"{field} must contain 1 to {maximum} characters"
        )
    return normalized


def _decision_id(value: object, *, field: str = "id") -> str:
    identifier = _text(value, field=field, maximum=68)
    if not _DECISION_ID_RE.fullmatch(identifier):
        raise RepositoryDecisionError(f"{field} must use the ADR- identifier format")
    return identifier


def _path(value: object, *, field: str) -> str:
    path = _text(value, field=field, maximum=_MAX_PATH_CHARS)
    if "\\" in path:
        raise RepositoryDecisionError(f"{field} must use forward slashes")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise RepositoryDecisionError(f"{field} must be a normalized repository path")
    return path


def _adr_path(value: object, *, field: str) -> str:
    path = _path(value, field=field)
    if not path.startswith(".review-agent/decisions/") or not path.endswith(".md"):
        raise RepositoryDecisionError(
            f"{field} must reference a Markdown file under .review-agent/decisions/"
        )
    return path


def _glob(value: object, *, field: str) -> str:
    pattern = _path(value, field=field)
    if any(character in pattern for character in "[]{}"):
        raise RepositoryDecisionError(
            f"{field} supports only simple glob syntax: *, ?, and ** path segments"
        )
    segments = pattern.split("/")
    for position, segment in enumerate(segments):
        if "**" in segment and segment != "**":
            raise RepositoryDecisionError(
                f"{field} may use ** only as a complete path segment"
            )
        if position and segment == "**" and segments[position - 1] == "**":
            raise RepositoryDecisionError(
                f"{field} must not contain consecutive ** segments"
            )
    return pattern


def _string_list(
    value: object,
    *,
    field: str,
    maximum_items: int,
    item_maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RepositoryDecisionError(
            f"{field} must contain 1 to {maximum_items} text items"
        )
    raw_items = cast(list[object], value)
    if not raw_items or len(raw_items) > maximum_items:
        raise RepositoryDecisionError(
            f"{field} must contain 1 to {maximum_items} text items"
        )
    items = tuple(
        _text(item, field=f"{field} item", maximum=item_maximum)
        for item in raw_items
    )
    if len(set(items)) != len(items):
        raise RepositoryDecisionError(f"{field} must not contain duplicates")
    return items


def parse_index(content: str) -> DecisionIndex:
    """Parse the one root path-to-ADR index with an exact schema."""
    if len(content.encode("utf-8")) > MAX_INDEX_BYTES:
        raise RepositoryDecisionError("decision index may contain at most 64 KiB")
    try:
        value = cast(dict[str, object], tomllib.loads(content))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise RepositoryDecisionError("decision index is not valid TOML") from exc
    if set(value) != {"version", "decision"}:
        raise RepositoryDecisionError(
            "decision index fields must be exactly version and decision"
        )
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise RepositoryDecisionError("decision index version must be 1")
    raw_entries = value.get("decision")
    if not isinstance(raw_entries, list):
        raise RepositoryDecisionError("decision must be an array of tables")
    entry_values = cast(list[object], raw_entries)
    if len(entry_values) > MAX_INDEX_ENTRIES:
        raise RepositoryDecisionError(
            f"decision index may contain at most {MAX_INDEX_ENTRIES} entries"
        )
    entries: list[DecisionIndexEntry] = []
    for position, raw_value in enumerate(entry_values, start=1):
        raw = cast(dict[str, object], raw_value) if isinstance(raw_value, dict) else None
        if raw is None or set(raw) != {"id", "adr_path", "applies_to"}:
            raise RepositoryDecisionError(
                f"decision entry {position} fields must be exactly id, adr_path, and applies_to"
            )
        globs = _string_list(
            raw.get("applies_to"),
            field=f"decision entry {position} applies_to",
            maximum_items=MAX_GLOBS_PER_DECISION,
            item_maximum=_MAX_PATH_CHARS,
        )
        entries.append(
            DecisionIndexEntry(
                id=_decision_id(raw.get("id"), field=f"decision entry {position} id"),
                adr_path=_adr_path(
                    raw.get("adr_path"),
                    field=f"decision entry {position} adr_path",
                ),
                applies_to=tuple(
                    _glob(item, field=f"decision entry {position} applies_to")
                    for item in globs
                ),
            )
        )
    ids = [entry.id for entry in entries]
    paths = [entry.adr_path for entry in entries]
    if len(set(ids)) != len(ids):
        raise RepositoryDecisionError("decision index IDs must be unique")
    if len(set(paths)) != len(paths):
        raise RepositoryDecisionError("decision index ADR paths must be unique")
    if sum(len(entry.applies_to) for entry in entries) > MAX_TOTAL_GLOBS:
        raise RepositoryDecisionError(
            f"decision index may contain at most {MAX_TOTAL_GLOBS} path patterns"
        )
    return DecisionIndex(entries=tuple(entries))


@lru_cache(maxsize=MAX_TOTAL_GLOBS)
def _compiled_glob(pattern: str) -> re.Pattern[str]:
    """Compile the validated path glob once for every matching run."""
    parts = pattern.split("/")
    expression: list[str] = ["^"]
    for index, part in enumerate(parts):
        if part == "**":
            if len(parts) == 1:
                expression.append(r"(?:[^/]+(?:/[^/]+)*)?")
            elif index == 0:
                expression.append(r"(?:[^/]+/)*")
            elif index == len(parts) - 1:
                expression.append(r"(?:/[^/]+)*")
            else:
                expression.append(r"/(?:[^/]+/)*")
            continue
        if index > 0 and parts[index - 1] != "**":
            expression.append("/")
        expression.append(
            re.escape(part).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        )
    expression.append("$")
    return re.compile("".join(expression))


def _matches(pattern: str, path: str) -> bool:
    return _compiled_glob(pattern).fullmatch(path) is not None


def matching_entries(
    index: DecisionIndex, *, changed_paths: tuple[str, ...]
) -> tuple[DecisionIndexMatch, ...]:
    """Return index entries relevant to at least one authoritative changed path."""
    matches: list[DecisionIndexMatch] = []
    unique_paths = tuple(dict.fromkeys(changed_paths))
    for entry in index.entries:
        matched_path_count = sum(
            any(_matches(pattern, path) for pattern in entry.applies_to)
            for path in unique_paths
        )
        if matched_path_count:
            matches.append(
                DecisionIndexMatch(
                    entry=entry,
                    matched_path_count=matched_path_count,
                )
            )
    return tuple(matches)


def decision_applies_to(decision: RepositoryDecision, *, path: str) -> bool:
    """Return whether one typed decision covers the normalized finding path."""
    normalized_path = _path(path, field="finding path")
    return any(_matches(pattern, normalized_path) for pattern in decision.applies_to)


def _metadata_hash(
    *,
    entry: DecisionIndexEntry,
    title: str,
    status: str,
    invariant: str,
    on_change: tuple[str, ...],
    evidence: str | None,
    origin_pr: int | None,
    supersedes: str | None,
) -> str:
    canonical = json.dumps(
        {
            "adr_path": entry.adr_path,
            "applies_to": list(entry.applies_to),
            "evidence": evidence,
            "id": entry.id,
            "invariant": invariant,
            "on_change": list(on_change),
            "origin_pr": origin_pr,
            "status": status,
            "supersedes": supersedes,
            "title": title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def snapshot_value(decision: RepositoryDecision) -> dict[str, object]:
    """Serialize one typed decision into the versioned run aggregate."""
    return {
        "id": decision.id,
        "adr_path": decision.adr_path,
        "applies_to": list(decision.applies_to),
        "title": decision.title,
        "status": decision.status,
        "invariant": decision.invariant,
        "on_change": list(decision.on_change),
        "evidence": decision.evidence,
        "origin_pr": decision.origin_pr,
        "supersedes": decision.supersedes,
        "invariant_line": decision.invariant_line,
        "matched_path_count": decision.matched_path_count,
        "metadata_hash": decision.metadata_hash,
    }


def restore_snapshot_value(value: object) -> RepositoryDecision:
    """Restore one stored decision while detecting malformed JSONB state."""
    if not isinstance(value, Mapping):
        raise RepositoryDecisionError("stored repository decision must be an object")
    item = cast(Mapping[str, object], value)
    expected = {
        "id",
        "adr_path",
        "applies_to",
        "title",
        "status",
        "invariant",
        "on_change",
        "evidence",
        "origin_pr",
        "supersedes",
        "invariant_line",
        "matched_path_count",
        "metadata_hash",
    }
    if set(item) != expected:
        raise RepositoryDecisionError("stored repository decision fields are invalid")
    identifier = _decision_id(item.get("id"))
    entry = DecisionIndexEntry(
        id=identifier,
        adr_path=_adr_path(item.get("adr_path"), field="adr_path"),
        applies_to=tuple(
            _glob(pattern, field="applies_to")
            for pattern in _string_list(
                item.get("applies_to"),
                field="applies_to",
                maximum_items=MAX_GLOBS_PER_DECISION,
                item_maximum=_MAX_PATH_CHARS,
            )
        ),
    )
    status = _text(item.get("status"), field="status", maximum=20)
    if status not in {"accepted", "superseded"}:
        raise RepositoryDecisionError("stored ADR status is invalid")
    title = _text(item.get("title"), field="title", maximum=300)
    invariant = _text(
        item.get("invariant"),
        field="invariant",
        maximum=_MAX_INVARIANT_CHARS,
    )
    on_change = _string_list(
        item.get("on_change"),
        field="on_change",
        maximum_items=MAX_ON_CHANGE_ITEMS,
        item_maximum=_MAX_ON_CHANGE_CHARS,
    )
    evidence_value = item.get("evidence")
    evidence = (
        _path(evidence_value, field="evidence")
        if evidence_value is not None
        else None
    )
    origin_pr_value = item.get("origin_pr")
    if origin_pr_value is None:
        origin_pr = None
    elif (
        isinstance(origin_pr_value, bool)
        or not isinstance(origin_pr_value, int)
        or origin_pr_value < 1
        or origin_pr_value > 2_147_483_647
    ):
        raise RepositoryDecisionError("stored origin_pr is invalid")
    else:
        origin_pr = origin_pr_value
    supersedes_value = item.get("supersedes")
    supersedes = (
        _decision_id(supersedes_value, field="supersedes")
        if supersedes_value is not None
        else None
    )
    invariant_line = item.get("invariant_line")
    if (
        isinstance(invariant_line, bool)
        or not isinstance(invariant_line, int)
        or invariant_line < 1
        or invariant_line > MAX_FRONTMATTER_LINES
    ):
        raise RepositoryDecisionError("stored invariant_line is invalid")
    matched_path_count = item.get("matched_path_count")
    if (
        isinstance(matched_path_count, bool)
        or not isinstance(matched_path_count, int)
        or matched_path_count < 1
    ):
        raise RepositoryDecisionError("stored matched_path_count is invalid")
    metadata_hash = item.get("metadata_hash")
    if not isinstance(metadata_hash, str) or not _HASH_RE.fullmatch(metadata_hash):
        raise RepositoryDecisionError("stored metadata_hash is invalid")
    expected_hash = _metadata_hash(
        entry=entry,
        title=title,
        status=status,
        invariant=invariant,
        on_change=on_change,
        evidence=evidence,
        origin_pr=origin_pr,
        supersedes=supersedes,
    )
    if metadata_hash != expected_hash:
        raise RepositoryDecisionError("stored metadata_hash does not match its decision")
    return RepositoryDecision(
        id=identifier,
        adr_path=entry.adr_path,
        applies_to=entry.applies_to,
        title=title,
        status=status,
        invariant=invariant,
        on_change=on_change,
        evidence=evidence,
        origin_pr=origin_pr,
        supersedes=supersedes,
        invariant_line=invariant_line,
        matched_path_count=matched_path_count,
        metadata_hash=metadata_hash,
    )


def parse_adr(content: str, *, match: DecisionIndexMatch) -> RepositoryDecision:
    """Parse the bounded TOML header of one indexed ADR."""
    entry = match.entry
    lines = content.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise RepositoryDecisionError("ADR TOML frontmatter must start on line 1")
    closing_line = next(
        (
            line_number
            for line_number, line in enumerate(
                lines[1:MAX_FRONTMATTER_LINES], start=2
            )
            if line.strip() == "+++"
        ),
        None,
    )
    if closing_line is None:
        raise RepositoryDecisionError(
            f"ADR TOML frontmatter must close within the first {MAX_FRONTMATTER_LINES} lines"
        )
    raw_frontmatter = "\n".join(lines[1 : closing_line - 1])
    if len(raw_frontmatter.encode("utf-8")) > MAX_ADR_HEADER_BYTES:
        raise RepositoryDecisionError("ADR TOML frontmatter may contain at most 32 KiB")
    try:
        value = cast(dict[str, object], tomllib.loads(raw_frontmatter))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise RepositoryDecisionError("ADR frontmatter is not valid TOML") from exc
    required = {"id", "title", "status", "invariant", "on_change"}
    optional = {"evidence", "origin_pr", "supersedes"}
    fields = set(value)
    if not required <= fields or fields - required - optional:
        raise RepositoryDecisionError(
            "ADR fields must be id, title, status, invariant, on_change, and optional evidence, origin_pr, or supersedes"
        )
    identifier = _decision_id(value.get("id"))
    if identifier != entry.id:
        raise RepositoryDecisionError("ADR id does not match the decision index")
    status = _text(value.get("status"), field="status", maximum=20)
    if status not in {"accepted", "superseded"}:
        raise RepositoryDecisionError("ADR status must be accepted or superseded")
    invariant_line = next(
        (
            line_number
            for line_number, line in enumerate(lines[1 : closing_line - 1], start=2)
            if _INVARIANT_LINE_RE.match(line)
        ),
        1,
    )
    evidence_value = value.get("evidence")
    evidence = (
        _path(evidence_value, field="evidence")
        if evidence_value is not None
        else None
    )
    origin_pr_value = value.get("origin_pr")
    if origin_pr_value is None:
        origin_pr = None
    elif (
        isinstance(origin_pr_value, bool)
        or not isinstance(origin_pr_value, int)
        or origin_pr_value < 1
        or origin_pr_value > 2_147_483_647
    ):
        raise RepositoryDecisionError("origin_pr must be a positive pull-request number")
    else:
        origin_pr = origin_pr_value
    supersedes_value = value.get("supersedes")
    supersedes = (
        _decision_id(supersedes_value, field="supersedes")
        if supersedes_value is not None
        else None
    )
    if supersedes == identifier:
        raise RepositoryDecisionError("supersedes must reference another ADR")
    title = _text(value.get("title"), field="title", maximum=300)
    invariant = _text(
        value.get("invariant"),
        field="invariant",
        maximum=_MAX_INVARIANT_CHARS,
    )
    on_change = _string_list(
        value.get("on_change"),
        field="on_change",
        maximum_items=MAX_ON_CHANGE_ITEMS,
        item_maximum=_MAX_ON_CHANGE_CHARS,
    )
    return RepositoryDecision(
        id=identifier,
        adr_path=entry.adr_path,
        applies_to=entry.applies_to,
        title=title,
        status=status,
        invariant=invariant,
        on_change=on_change,
        evidence=evidence,
        origin_pr=origin_pr,
        supersedes=supersedes,
        invariant_line=invariant_line,
        matched_path_count=match.matched_path_count,
        metadata_hash=_metadata_hash(
            entry=entry,
            title=title,
            status=status,
            invariant=invariant,
            on_change=on_change,
            evidence=evidence,
            origin_pr=origin_pr,
            supersedes=supersedes,
        ),
    )


__all__ = [
    "DecisionIndex",
    "DecisionIndexEntry",
    "DecisionIndexMatch",
    "MAX_ADR_HEADER_BYTES",
    "MAX_FRONTMATTER_LINES",
    "MAX_GLOBS_PER_DECISION",
    "MAX_INDEX_BYTES",
    "MAX_INDEX_ENTRIES",
    "MAX_INDEX_LINES",
    "MAX_MATCHED_DECISIONS",
    "MAX_ON_CHANGE_ITEMS",
    "MAX_TOTAL_GLOBS",
    "RepositoryDecision",
    "RepositoryDecisionError",
    "decision_applies_to",
    "matching_entries",
    "parse_adr",
    "parse_index",
    "restore_snapshot_value",
    "snapshot_value",
]
