"""Bounded GitHub source operations for one durable review subject."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
from typing import Any, Mapping, cast
import urllib.parse

from .. import changed_files
from ..postgres.review_runs import ReviewRunScope
from ..source_control import GitHubReadClient, GitHubReadError


JsonObject = dict[str, Any]
# The contents API inlines files up to 1 MiB. Larger files use the raw-media
# endpoint, whose response must stay within one gateway request's memory budget.
# This bounds one source read, not the number of files or total review depth.
_GITHUB_RAW_FILE_MAX_BYTES = 2_000_000


class GitHubSourceError(ValueError):
    """GitHub returned source data that does not match the durable subject."""


@dataclass(frozen=True, slots=True)
class ReviewPullSource:
    repository: str
    pr_number: int
    payload: JsonObject

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewPullSource":
        if set(value) != {"kind", "repository", "pr_number", "payload"}:
            raise GitHubSourceError("gateway pull response has unexpected fields")
        if value.get("kind") != "pull":
            raise GitHubSourceError("gateway pull response has the wrong kind")
        repository = value.get("repository")
        pr_number = value.get("pr_number")
        payload = value.get("payload")
        if not isinstance(repository, str) or not repository.strip():
            raise GitHubSourceError("gateway pull response has no repository")
        return cls(
            repository=repository,
            pr_number=_positive(pr_number, "gateway pull response has no PR number"),
            payload=dict(_object(payload, "gateway pull response has no payload")),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "pull",
            "repository": self.repository,
            "pr_number": self.pr_number,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ReviewSourceBytes:
    state: str
    body: bytes
    truncated: bool
    headers: dict[str, str]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewSourceBytes":
        if set(value) != {"kind", "state", "body", "truncated", "headers"}:
            raise GitHubSourceError("gateway byte response has unexpected fields")
        if value.get("kind") != "bytes":
            raise GitHubSourceError("gateway byte response has the wrong kind")
        encoded = value.get("body")
        state = value.get("state")
        truncated = value.get("truncated")
        headers = value.get("headers")
        if (
            not isinstance(state, str)
            or not isinstance(encoded, str)
            or not isinstance(truncated, bool)
        ):
            raise GitHubSourceError("gateway byte response is invalid")
        if not isinstance(headers, Mapping):
            raise GitHubSourceError("gateway byte response headers are invalid")
        raw_headers = cast(Mapping[object, object], headers)
        if not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in raw_headers.items()
        ):
            raise GitHubSourceError("gateway byte response headers are invalid")
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GitHubSourceError("gateway byte response body is invalid") from exc
        return cls(
            state=state,
            body=body,
            truncated=truncated,
            headers={cast(str, key): cast(str, item) for key, item in raw_headers.items()},
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "bytes",
            "state": self.state,
            "body": base64.b64encode(self.body).decode("ascii"),
            "truncated": self.truncated,
            "headers": self.headers,
        }


@dataclass(frozen=True, slots=True)
class ReviewFilePage:
    state: str
    repository: str
    revision: str
    start_line: int
    total_lines: int
    content: str
    complete_lines: int
    partial_line: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewFilePage":
        expected = {
            "kind",
            "state",
            "repository",
            "revision",
            "start_line",
            "total_lines",
            "content",
            "complete_lines",
            "partial_line",
        }
        if set(value) != expected or value.get("kind") != "file_page":
            raise GitHubSourceError("gateway file response has unexpected fields")
        text_fields = ("state", "repository", "revision", "content")
        if not all(isinstance(value.get(field), str) for field in text_fields):
            raise GitHubSourceError("gateway file response text is invalid")
        if not isinstance(value.get("partial_line"), bool):
            raise GitHubSourceError("gateway file response partial state is invalid")
        start_line = value.get("start_line")
        total_lines = value.get("total_lines")
        complete_lines = value.get("complete_lines")
        if type(start_line) is not int or start_line < 1:
            raise GitHubSourceError("gateway file response start line is invalid")
        if type(total_lines) is not int or total_lines < 0:
            raise GitHubSourceError("gateway file response line count is invalid")
        if type(complete_lines) is not int or complete_lines < 0:
            raise GitHubSourceError("gateway file response page count is invalid")
        return cls(
            state=cast(str, value["state"]),
            repository=cast(str, value["repository"]),
            revision=cast(str, value["revision"]),
            start_line=start_line,
            total_lines=total_lines,
            content=cast(str, value["content"]),
            complete_lines=complete_lines,
            partial_line=cast(bool, value["partial_line"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "file_page",
            "state": self.state,
            "repository": self.repository,
            "revision": self.revision,
            "start_line": self.start_line,
            "total_lines": self.total_lines,
            "content": self.content,
            "complete_lines": self.complete_lines,
            "partial_line": self.partial_line,
        }


def _object(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubSourceError(message)
    return cast(Mapping[str, object], value)


def _positive(value: object, message: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubSourceError(message)
    return value


def _nonnegative(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _text(value: object, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def read_review_pull(
    github: GitHubReadClient,
    scope: ReviewRunScope,
) -> ReviewPullSource:
    """Read and sanitize the current PR while preserving its exact stable identity."""
    repository = urllib.parse.quote(scope.repository, safe="/")
    value = github.request_json(
        f"/repos/{repository}/pulls/{scope.pr_number}",
        max_bytes=2_000_000,
    )
    pull = _object(value, "GitHub returned an invalid pull request")
    base = _object(pull.get("base"), "GitHub returned an invalid pull request base")
    head = _object(pull.get("head"), "GitHub returned an invalid pull request head")
    base_repository = _object(
        base.get("repo"), "GitHub returned an invalid base repository"
    )
    head_repository = _object(
        head.get("repo"), "GitHub returned an invalid head repository"
    )
    if _positive(base_repository.get("id"), "invalid base repository id") != (
        scope.provider_repository_id
    ):
        raise GitHubSourceError("pull request repository identity changed")
    if _positive(head_repository.get("id"), "invalid head repository id") != (
        scope.provider_repository_id
    ):
        raise GitHubSourceError("fork source is not supported")
    payload: JsonObject = {
        "state": _text(pull.get("state"), 20),
        "draft": bool(pull.get("draft")),
        "title": _text(pull.get("title"), 300),
        "html_url": _text(pull.get("html_url"), 500),
        "user": {
            "login": _text(
                _object(pull.get("user"), "invalid pull author").get("login"),
                100,
            )
        },
        "base": {
            "sha": _text(base.get("sha"), 128).lower(),
            "ref": _text(base.get("ref"), 200),
            "repo": {
                "id": scope.provider_repository_id,
                "full_name": _text(base_repository.get("full_name"), 260),
            },
        },
        "head": {
            "sha": _text(head.get("sha"), 128).lower(),
            "ref": _text(head.get("ref"), 200),
            "repo": {
                "id": scope.provider_repository_id,
                "full_name": _text(head_repository.get("full_name"), 260),
            },
        },
        "changed_files": _nonnegative(pull.get("changed_files")),
        "additions": _nonnegative(pull.get("additions")),
        "deletions": _nonnegative(pull.get("deletions")),
    }
    return ReviewPullSource(
        repository=scope.repository,
        pr_number=scope.pr_number,
        payload=payload,
    )


def read_changed_files_page(
    github: GitHubReadClient,
    scope: ReviewRunScope,
    *,
    per_page: int,
    page: int,
) -> ReviewSourceBytes:
    repository = urllib.parse.quote(scope.repository, safe="/")
    body, truncated, headers = github.request(
        f"/repos/{repository}/pulls/{scope.pr_number}/files"
        f"?per_page={per_page}&page={page}",
        max_bytes=changed_files.ENUMERATION_MAX_BYTES,
    )
    return ReviewSourceBytes(
        state="ok", body=body, truncated=truncated, headers=headers
    )


def read_review_diff(
    github: GitHubReadClient,
    scope: ReviewRunScope,
) -> ReviewSourceBytes:
    repository = urllib.parse.quote(scope.repository, safe="/")
    try:
        body, truncated, headers = github.request(
            f"/repos/{repository}/pulls/{scope.pr_number}",
            accept="application/vnd.github.v3.diff",
            max_bytes=1_000_000,
        )
    except GitHubReadError as exc:
        if exc.kind == "diff_unavailable":
            return ReviewSourceBytes(
                state="diff_unavailable",
                body=b"",
                truncated=False,
                headers={},
            )
        raise
    return ReviewSourceBytes(
        state="ok", body=body, truncated=truncated, headers=headers
    )


def _terminal_file(
    scope: ReviewRunScope,
    side: str,
    state: str,
    start_line: int,
) -> ReviewFilePage:
    return ReviewFilePage(
        state=state,
        repository=scope.repository,
        revision=scope.head_sha if side == "head" else scope.base_sha,
        start_line=start_line,
        total_lines=0,
        content="",
        complete_lines=0,
        partial_line=False,
    )


def read_review_file_page(
    github: GitHubReadClient,
    scope: ReviewRunScope,
    *,
    path: str,
    side: str,
    start_line: int,
    max_lines: int,
    max_chars: int,
) -> ReviewFilePage:
    """Return one bounded source page without returning the complete file to Hermes."""
    repository = urllib.parse.quote(scope.repository, safe="/")
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in path.split("/")
    )
    revision = scope.head_sha if side == "head" else scope.base_sha
    ref = urllib.parse.quote(revision, safe="")
    endpoint = f"/repos/{repository}/contents/{encoded_path}?ref={ref}"
    try:
        value = github.request_json(endpoint, max_bytes=2_000_000)
    except GitHubReadError as exc:
        if exc.kind == "not_found":
            return _terminal_file(scope, side, "not_found_at_revision", start_line)
        raise
    metadata = _object(value, "GitHub returned invalid file metadata")
    if metadata.get("type") != "file":
        return _terminal_file(scope, side, "not_regular", start_line)
    raw_content = metadata.get("content")
    if metadata.get("encoding") == "base64" and isinstance(raw_content, str):
        try:
            raw = base64.b64decode(raw_content, validate=False)
        except (ValueError, binascii.Error) as exc:
            raise GitHubSourceError("GitHub returned invalid file content") from exc
    else:
        size = metadata.get("size")
        if type(size) is not int or size > _GITHUB_RAW_FILE_MAX_BYTES:
            return _terminal_file(scope, side, "too_large", start_line)
        raw, truncated, _ = github.request(
            endpoint,
            accept="application/vnd.github.raw+json",
            max_bytes=_GITHUB_RAW_FILE_MAX_BYTES,
        )
        if truncated:
            return _terminal_file(scope, side, "too_large", start_line)
    if b"\x00" in raw[:8192]:
        return _terminal_file(scope, side, "binary", start_line)
    # Preserve line boundaries without accepting invalid bytes. Surrogate escapes
    # let a bounded page ignore invalid data outside the requested page while the
    # fragment actually returned below still fails closed.
    lines = raw.decode("utf-8", errors="surrogateescape").splitlines()
    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    parts: list[str] = []
    used = 0
    complete_lines = 0
    partial_line = False
    for line_number, line in enumerate(selected, start=start_line):
        rendered = f"{line_number}: {line}"
        candidate = ("\n" if parts else "") + rendered
        remaining = max_chars - used
        if len(candidate) <= remaining:
            if any(0xDC80 <= ord(character) <= 0xDCFF for character in candidate):
                return _terminal_file(scope, side, "not_utf8", start_line)
            parts.append(candidate)
            used += len(candidate)
            complete_lines += 1
            continue
        fragment = candidate[:remaining]
        if fragment:
            if any(0xDC80 <= ord(character) <= 0xDCFF for character in fragment):
                return _terminal_file(scope, side, "not_utf8", start_line)
            parts.append(fragment)
            partial_line = True
        break
    return ReviewFilePage(
        state="ok",
        repository=scope.repository,
        revision=revision,
        start_line=start_line,
        total_lines=len(lines),
        content="".join(parts),
        complete_lines=complete_lines,
        partial_line=partial_line,
    )


__all__ = [
    "GitHubReadError",
    "GitHubSourceError",
    "ReviewFilePage",
    "ReviewPullSource",
    "ReviewSourceBytes",
    "read_changed_files_page",
    "read_review_diff",
    "read_review_file_page",
    "read_review_pull",
]
