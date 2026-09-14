"""Bounded GitHub source operations for one durable review subject."""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from threading import Lock
import base64
import binascii
import hashlib
import re
from typing import Any, Mapping, cast
import urllib.parse

from .. import changed_files
from ..domain.review import ReviewPurpose
from ..domain.documentation_policy import CONFIG_PATH, MAX_CONFIG_BYTES
from ..postgres.review_runs import ReviewRunScope
from ..source_control import GitHubReadClient, GitHubReadError


JsonObject = dict[str, Any]
# The contents API inlines files up to 1 MiB. Larger files use the raw-media
# endpoint, whose response must stay within one gateway request's memory budget.
# This bounds one source read, not the number of files or total review depth.
_GITHUB_RAW_FILE_MAX_BYTES = 2_000_000
# Retain at most two maximum-size files: under 1.5% of the gateway's 256 MiB
# budget, separate from concurrent request allocations. Entry count bounds keys.
_FILE_CACHE_MAX_BYTES = 2 * _GITHUB_RAW_FILE_MAX_BYTES
_FILE_CACHE_MAX_ENTRIES = 32


class GitHubSourceError(ValueError):
    """GitHub returned source data that does not match the durable subject."""


class GitHubComparisonUnavailable(GitHubSourceError):
    """The exact commits have no merge base available for comparison."""


def _commit_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise GitHubSourceError("GitHub returned an invalid source identity")
    return value


@dataclass(frozen=True, slots=True)
class ReviewComparison:
    base_sha: str
    head_sha: str
    comparison_sha: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewComparison":
        if set(value) != {"kind", "base_sha", "head_sha", "comparison_sha"} or value.get("kind") != "comparison":
            raise GitHubSourceError("gateway comparison response has unexpected fields")
        return cls(*(_commit_sha(value[key]) for key in ("base_sha", "head_sha", "comparison_sha")))

    def to_mapping(self) -> dict[str, object]:
        return {"kind": "comparison", "base_sha": self.base_sha,
                "head_sha": self.head_sha, "comparison_sha": self.comparison_sha}


@dataclass(frozen=True, slots=True)
class ReviewPolicySource:
    state: str
    revision: str
    content: str | None
    blob_sha: str | None
    content_sha256: str | None

    def to_mapping(self) -> dict[str, object]:
        return {"kind": "documentation_policy", "state": self.state,
                "revision": self.revision, "content": self.content,
                "blob_sha": self.blob_sha, "content_sha256": self.content_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewPolicySource":
        if set(value) != {"kind", "state", "revision", "content", "blob_sha", "content_sha256"} or value.get("kind") != "documentation_policy":
            raise GitHubSourceError("gateway documentation policy has unexpected fields")
        state = value["state"]
        content = value["content"]
        digest = value["content_sha256"]
        blob = value["blob_sha"]
        if not isinstance(state, str):
            raise GitHubSourceError("gateway documentation policy state is invalid")
        if state == "ok":
            if (not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONFIG_BYTES
                or not isinstance(digest, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest):
                raise GitHubSourceError("gateway documentation policy content is invalid")
            blob = _commit_sha(blob)
        elif content is not None or blob is not None or digest is not None:
            raise GitHubSourceError("unavailable documentation policy contains content")
        return cls(state, _commit_sha(value["revision"]), content, blob, digest)


@dataclass(frozen=True, slots=True)
class ReviewFileKey:
    provider_repository_id: int
    commit_sha: str
    path: str


class ReviewFileCache:
    """Process-local immutable bytes shared by concurrent gateway requests.

    Every access still requires fresh gateway authority and token checks.
    Eviction or process exit releases entries; misses may fetch concurrently.
    """

    def __init__(
        self, *, max_bytes: int = _FILE_CACHE_MAX_BYTES,
        max_entries: int = _FILE_CACHE_MAX_ENTRIES,
    ) -> None:
        if max_bytes < 1 or max_entries < 1:
            raise ValueError("file cache bounds must be positive")
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._bytes = 0
        self._entries: OrderedDict[ReviewFileKey, bytes] = OrderedDict()
        self._lock = Lock()

    def get(self, key: ReviewFileKey) -> bytes | None:
        with self._lock:
            raw = self._entries.get(key)
            if raw is not None:
                self._entries.move_to_end(key)
            return raw

    def put(self, key: ReviewFileKey, raw: bytes) -> None:
        if len(raw) > self._max_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous)
            self._entries[key] = raw
            self._bytes += len(raw)
            while self._bytes > self._max_bytes or len(self._entries) > self._max_entries:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)


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
    blob_sha: str | None = None

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
            "blob_sha",
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
            blob_sha=_commit_sha(value["blob_sha"]) if value["blob_sha"] is not None else None,
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
            "blob_sha": self.blob_sha,
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
    revision: str,
    state: str,
    start_line: int,
) -> ReviewFilePage:
    return ReviewFilePage(
        state=state,
        repository=scope.repository,
        revision=revision,
        start_line=start_line,
        total_lines=0,
        content="",
        complete_lines=0,
        partial_line=False,
    )


def _regular_file_blob(
    github: GitHubReadClient, *, repository: str, revision: str, path: str,
) -> tuple[str | None, str | None]:
    """Check exact tree modes because the contents API follows repository symlinks."""
    segments = path.split("/")
    if len(segments) > 32:
        return None, "path_too_deep"
    commit = _object(github.request_json(
        f"/repos/{repository}/git/commits/{revision}", max_bytes=1_000_000,
    ), "GitHub returned invalid commit metadata")
    if _commit_sha(commit.get("sha")) != revision:
        raise GitHubSourceError("GitHub file commit does not match its revision")
    tree_sha = _commit_sha(_object(commit.get("tree"), "GitHub commit has no tree").get("sha"))
    for index, segment in enumerate(segments):
        tree = _object(github.request_json(
            f"/repos/{repository}/git/trees/{tree_sha}", max_bytes=2_000_000,
        ), "GitHub returned invalid tree metadata")
        if _commit_sha(tree.get("sha")) != tree_sha:
            raise GitHubSourceError("GitHub returned a different source tree")
        if tree.get("truncated") is not False:
            return None, "tree_incomplete"
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubSourceError("GitHub tree has no entries")
        matches = [entry for raw_entry in cast(list[object], entries)
                   if (entry := _object(raw_entry, "GitHub tree entry is invalid")).get("path") == segment]
        if not matches:
            return None, "not_found_at_revision"
        if len(matches) != 1:
            raise GitHubSourceError("GitHub source path is ambiguous")
        entry = _object(matches[0], "GitHub returned an invalid tree entry")
        last = index == len(segments) - 1
        if last and entry.get("type") == "blob" and entry.get("mode") in {"100644", "100755"}:
            return _commit_sha(entry.get("sha")), None
        if last or entry.get("type") != "tree" or entry.get("mode") != "040000":
            return None, "not_regular"
        tree_sha = _commit_sha(entry.get("sha"))
    return None, "not_found_at_revision"


def read_review_file_page(
    github: GitHubReadClient,
    scope: ReviewRunScope,
    *,
    path: str,
    side: str,
    start_line: int,
    max_lines: int,
    max_chars: int,
    cache: ReviewFileCache | None = None,
    comparison_sha: str | None = None,
    require_regular: bool = False,
) -> ReviewFilePage:
    """Return one bounded source page without returning the complete file to Hermes."""
    repository = urllib.parse.quote(scope.repository, safe="/")
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in path.split("/")
    )
    if side == "comparison":
        if scope.run.purpose is not ReviewPurpose.DOCUMENTATION or comparison_sha is None:
            raise GitHubSourceError("comparison reads require a documentation review identity")
        revision = _commit_sha(comparison_sha)
    elif side in {"head", "base"}:
        revision = scope.head_sha if side == "head" else scope.base_sha
    else:
        raise GitHubSourceError("source revision role is invalid")
    expected_blob = None
    if require_regular or side == "comparison":
        expected_blob, unavailable = _regular_file_blob(
            github, repository=repository, revision=revision, path=path,
        )
        if unavailable is not None:
            return _terminal_file(scope, revision, unavailable, start_line)
    ref = urllib.parse.quote(revision, safe="")
    endpoint = f"/repos/{repository}/contents/{encoded_path}?ref={ref}"
    key = ReviewFileKey(
        provider_repository_id=scope.provider_repository_id,
        commit_sha=revision,
        path=path,
    )
    raw = cache.get(key) if cache is not None else None
    if raw is None:
        try:
            value = github.request_json(endpoint, max_bytes=2_000_000)
        except GitHubReadError as exc:
            if exc.kind == "not_found":
                return _terminal_file(scope, revision, "not_found_at_revision", start_line)
            raise
        metadata = _object(value, "GitHub returned invalid file metadata")
        if metadata.get("type") != "file":
            return _terminal_file(scope, revision, "not_regular", start_line)
        raw_content = metadata.get("content")
        if metadata.get("encoding") == "base64" and isinstance(raw_content, str):
            try:
                raw = base64.b64decode(raw_content, validate=False)
            except (ValueError, binascii.Error) as exc:
                raise GitHubSourceError("GitHub returned invalid file content") from exc
        else:
            size = metadata.get("size")
            if type(size) is not int or size > _GITHUB_RAW_FILE_MAX_BYTES:
                return _terminal_file(scope, revision, "too_large", start_line)
            raw, truncated, _ = github.request(
                endpoint,
                accept="application/vnd.github.raw+json",
                max_bytes=_GITHUB_RAW_FILE_MAX_BYTES,
            )
            if truncated:
                return _terminal_file(scope, revision, "too_large", start_line)
        if b"\x00" in raw[:8192]:
            return _terminal_file(scope, revision, "binary", start_line)
    blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if expected_blob is not None and expected_blob != blob_sha:
        raise GitHubSourceError("GitHub file bytes do not match the exact tree blob")
    if cache is not None:
        cache.put(key, raw)
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
                return _terminal_file(scope, revision, "not_utf8", start_line)
            parts.append(candidate)
            used += len(candidate)
            complete_lines += 1
            continue
        fragment = candidate[:remaining]
        if fragment:
            if any(0xDC80 <= ord(character) <= 0xDCFF for character in fragment):
                return _terminal_file(scope, revision, "not_utf8", start_line)
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
        blob_sha=blob_sha,
    )


def read_review_comparison(github: GitHubReadClient, scope: ReviewRunScope) -> ReviewComparison:
    """Resolve the merge base of the stored exact commits without changing base policy authority."""
    repository = urllib.parse.quote(scope.repository, safe="/")
    result = _object(github.request_json(
        f"/repos/{repository}/compare/{scope.base_sha}...{scope.head_sha}?per_page=1",
        max_bytes=2_000_000,
    ), "GitHub returned an invalid comparison")
    base = _object(result.get("base_commit"), "GitHub comparison has no base commit")
    if _commit_sha(base.get("sha")) != scope.base_sha:
        raise GitHubSourceError("GitHub comparison does not match the requested base")
    if result.get("merge_base_commit") is None:
        raise GitHubComparisonUnavailable("GitHub comparison has no merge base")
    comparison = _object(result["merge_base_commit"], "GitHub comparison returned an invalid merge base")
    return ReviewComparison(scope.base_sha, scope.head_sha, _commit_sha(comparison.get("sha")))


def read_documentation_policy(
    github: GitHubReadClient, scope: ReviewRunScope, *, side: str,
) -> ReviewPolicySource:
    """Read the fixed policy as exact UTF-8, without normalizing TOML string content."""
    if scope.run.purpose is not ReviewPurpose.DOCUMENTATION or side not in {"head", "base"}:
        raise GitHubSourceError("documentation policy requires an exact documentation subject")
    revision = scope.base_sha if side == "base" else scope.head_sha
    return read_documentation_policy_at_revision(github, repository=scope.repository, revision=revision)


def read_documentation_policy_at_revision(
    github: GitHubReadClient, *, repository: str, revision: str,
) -> ReviewPolicySource:
    """Read policy bytes for an already-authorized immutable repository revision."""
    revision = _commit_sha(revision)
    repository = urllib.parse.quote(repository, safe="/")
    blob, unavailable = _regular_file_blob(
        github, repository=repository, revision=revision, path=CONFIG_PATH,
    )
    if unavailable is not None:
        return ReviewPolicySource(unavailable, revision, None, None, None)
    raw, truncated, _ = github.request(
        f"/repos/{repository}/contents/{CONFIG_PATH}?ref={revision}",
        accept="application/vnd.github.raw+json", max_bytes=MAX_CONFIG_BYTES,
    )
    if truncated or len(raw) > MAX_CONFIG_BYTES:
        return ReviewPolicySource("too_large", revision, None, None, None)
    actual_blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if actual_blob != blob:
        raise GitHubSourceError("documentation policy bytes do not match the exact tree blob")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ReviewPolicySource("not_utf8", revision, None, None, None)
    return ReviewPolicySource("ok", revision, content, blob, hashlib.sha256(raw).hexdigest())


__all__ = [
    "ReviewComparison",
    "ReviewPolicySource",
    "ReviewFileCache",
    "ReviewFileKey",
    "GitHubReadError",
    "GitHubSourceError",
    "ReviewFilePage",
    "ReviewPullSource",
    "ReviewSourceBytes",
    "read_changed_files_page",
    "read_review_diff",
    "read_review_file_page",
    "read_review_pull",
    "read_review_comparison",
    "read_documentation_policy",
    "read_documentation_policy_at_revision",
]
