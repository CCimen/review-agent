"""Deterministic documentation scope planning from bounded local Git objects."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import selectors
import subprocess
import time

from .domain import documentation_policy, repository_paths


MAX_CHANGED_FILES = 3_000
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_GIT_ERROR_CHARS = 300
MAX_REF_CHARS = 200
GIT_TIMEOUT_SECONDS = 10
_POLICY_OBJECT_ERROR_CODES = {
    "git_object_ambiguous",
    "git_object_incomplete",
    "git_object_invalid",
    "git_object_too_large",
}
_INHERITED_GIT_AUTHORITY_VARIABLES = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
)


class DocumentationScopeError(ValueError):
    """Local Git evidence cannot produce a trustworthy scope preview."""

    code: str
    detail: str | None

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:MAX_GIT_ERROR_CHARS] if detail else None


@dataclass(frozen=True, slots=True)
class ChangedPath:
    status: str
    path: str
    previous_path: str | None = None

    def paths(self) -> tuple[str, ...]:
        return (self.path,) if self.previous_path is None else (self.path, self.previous_path)

    def to_json_obj(self) -> dict[str, str | None]:
        return {"path": self.path, "previous_path": self.previous_path, "status": self.status}


@dataclass(frozen=True, slots=True)
class SelectedArea:
    id: str
    intent: str
    matched_paths: tuple[str, ...]
    documents: tuple[str, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "documents": list(self.documents), "id": self.id, "intent": self.intent,
            "matched_paths": list(self.matched_paths),
        }


@dataclass(frozen=True, slots=True)
class ScopeExclusion:
    path: str
    kind: str
    reason: str

    def to_json_obj(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class DocumentationScope:
    base_sha: str
    comparison_sha: str | None
    head_sha: str
    status: str
    active_policy: bool
    policy_hash: str | None
    proposal_status: str
    proposal_detail: str | None
    changed_files: tuple[ChangedPath, ...]
    areas: tuple[SelectedArea, ...]
    documents: tuple[str, ...]
    exclusions: tuple[ScopeExclusion, ...]
    unmapped_paths: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    semantic_inference_used: bool = False

    def to_json_obj(self) -> dict[str, object]:
        return {
            "active_policy": self.active_policy, "areas": [item.to_json_obj() for item in self.areas],
            "base_sha": self.base_sha, "changed_files": [item.to_json_obj() for item in self.changed_files],
            "comparison_sha": self.comparison_sha, "documents": list(self.documents),
            "exclusions": [item.to_json_obj() for item in self.exclusions],
            "head_sha": self.head_sha, "incomplete_reasons": list(self.incomplete_reasons),
            "policy_hash": self.policy_hash, "proposal_detail": self.proposal_detail,
            "proposal_status": self.proposal_status, "schema_version": 1,
            "semantic_inference_used": self.semantic_inference_used, "status": self.status,
            "unmapped_paths": list(self.unmapped_paths),
        }


def _run_git(root: Path, arguments: tuple[str, ...], *, allow_failure: bool = False) -> bytes | None:
    environment = os.environ.copy()
    environment.pop("GIT_EXTERNAL_DIFF", None)
    environment.pop("GIT_DIFF_OPTS", None)
    for name in _INHERITED_GIT_AUTHORITY_VARIABLES:
        environment.pop(name, None)
    environment["GIT_ALLOW_PROTOCOL"] = ""
    environment["GIT_NO_LAZY_FETCH"] = "1"
    try:
        process = subprocess.Popen(
            (
                "git",
                "--literal-pathspecs",
                "--no-replace-objects",
                "-C",
                str(root),
                *arguments,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
    except OSError as exc:
        raise DocumentationScopeError("git_unavailable") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    output = bytearray()
    error = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, output)
    selector.register(process.stderr, selectors.EVENT_READ, error)
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    failure_code: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure_code = "git_timeout"
                break
            for key, _ in selector.select(timeout=min(remaining, 0.1)):
                destination = key.data
                assert isinstance(destination, bytearray)
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                destination.extend(chunk)
                if len(destination) > MAX_GIT_OUTPUT_BYTES:
                    failure_code = "git_output_too_large"
                    break
            if failure_code is not None:
                break
    finally:
        selector.close()
        if failure_code is not None and process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    if failure_code is not None:
        raise DocumentationScopeError(failure_code)
    if process.returncode:
        if allow_failure:
            return None
        detail = bytes(error).decode("utf-8", errors="replace").strip()
        raise DocumentationScopeError("git_command_failed", detail)
    return bytes(output)


def _safe_ref(ref: str, *, field: str) -> str:
    if not ref or ref.startswith("-") or "\x00" in ref or len(ref) > MAX_REF_CHARS:
        raise DocumentationScopeError("git_ref_invalid", field)
    return ref


def _resolve(root: Path, ref: str) -> str:
    output = _run_git(root, ("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"))
    assert output is not None
    sha = output.decode("ascii", errors="strict").strip()
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise DocumentationScopeError("git_revision_invalid")
    return sha


def _blob(root: Path, revision: str, path: str, *, maximum: int) -> bytes | None:
    inventory = _run_git(root, ("ls-tree", "-z", "--full-tree", revision, "--", path))
    assert inventory is not None
    if not inventory:
        return None
    entries = [item for item in inventory.split(b"\x00") if item]
    if len(entries) != 1:
        raise DocumentationScopeError("git_object_ambiguous", path)
    metadata, separator, raw_path = entries[0].partition(b"\t")
    if not separator or raw_path.decode("utf-8", errors="strict") != path:
        raise DocumentationScopeError("git_object_invalid", path)
    fields = metadata.split(b" ")
    if len(fields) != 3 or fields[1] != b"blob" or fields[0] not in {b"100644", b"100755"}:
        raise DocumentationScopeError("git_object_invalid", path)
    size_output = _run_git(root, ("cat-file", "-s", fields[2].decode("ascii")))
    assert size_output is not None
    try:
        size = int(size_output)
    except ValueError as exc:
        raise DocumentationScopeError("git_object_invalid", path) from exc
    if size > maximum:
        raise DocumentationScopeError("git_object_too_large", path)
    content = _run_git(root, ("cat-file", "blob", fields[2].decode("ascii")))
    assert content is not None
    if len(content) != size:
        raise DocumentationScopeError("git_object_incomplete", path)
    return content


def _changed_files(root: Path, comparison_sha: str, head_sha: str) -> tuple[ChangedPath, ...]:
    output = _run_git(
        root,
        ("-c", "diff.external=", "diff", "--no-ext-diff", "--no-textconv",
         "--find-renames", "--name-status", "-z", comparison_sha, head_sha, "--"),
    )
    assert output is not None
    fields = output.split(b"\x00")
    if fields and not fields[-1]:
        fields.pop()
    result: list[ChangedPath] = []
    position = 0
    try:
        while position < len(fields):
            status = fields[position].decode("ascii")
            position += 1
            kind = status[:1]
            if kind in {"R", "C"}:
                old = fields[position].decode("utf-8")
                new = fields[position + 1].decode("utf-8")
                position += 2
                result.append(ChangedPath(status, new, old))
            elif kind in {"A", "M", "D", "T", "U"}:
                path = fields[position].decode("utf-8")
                position += 1
                result.append(ChangedPath(status, path))
            else:
                raise DocumentationScopeError("git_change_status_invalid", status)
    except (IndexError, UnicodeDecodeError) as exc:
        raise DocumentationScopeError("git_change_inventory_invalid") from exc
    if len(result) > MAX_CHANGED_FILES:
        raise DocumentationScopeError("git_change_inventory_too_large")
    return tuple(result)


def _decode_policy(content: bytes, *, role: str) -> documentation_policy.DocumentationPolicy:
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentationScopeError(f"{role}_policy_invalid", "policy is not UTF-8") from exc
    try:
        return documentation_policy.parse_policy(decoded)
    except documentation_policy.DocumentationPolicyError as exc:
        raise DocumentationScopeError(f"{role}_policy_invalid", str(exc)) from exc


def _invalid_base_scope(
    *,
    base_sha: str,
    comparison_sha: str,
    head_sha: str,
    changed_files: tuple[ChangedPath, ...],
    policy_hash: str | None,
    detail: str,
    proposal_status: str = "unavailable",
    proposal_detail: str | None = None,
) -> DocumentationScope:
    return DocumentationScope(
        base_sha, comparison_sha, head_sha, "invalid_configuration", False,
        policy_hash, proposal_status, proposal_detail, changed_files, (), (), (), (),
        (detail,),
    )


def plan_scope(
    *, base_sha: str, comparison_sha: str | None, head_sha: str,
    changed_files: tuple[ChangedPath, ...], policy: documentation_policy.DocumentationPolicy | None,
    policy_hash: str | None, proposal_status: str = "unchanged", proposal_detail: str | None = None,
) -> DocumentationScope:
    """Plan scope from already authoritative refs, inventory, and accepted policy."""
    if policy is None:
        return DocumentationScope(
            base_sha, comparison_sha, head_sha, "not_configured", False, None,
            proposal_status, proposal_detail, changed_files, (), (), (),
            tuple(dict.fromkeys(path for item in changed_files for path in item.paths())), (),
        )
    path_order = tuple(dict.fromkeys(path for item in changed_files for path in item.paths()))
    areas: list[SelectedArea] = []
    mapped: set[str] = set()
    documents: list[str] = []
    for area in policy.areas:
        matched = tuple(
            path for path in path_order
            if path in area.documents or any(repository_paths.matches(pattern, path) for pattern in area.sources)
        )
        if matched:
            mapped.update(matched)
            areas.append(SelectedArea(area.id, area.intent, matched, area.documents))
            for document in area.documents:
                if document not in documents:
                    documents.append(document)
    exclusions: list[ScopeExclusion] = []
    unmapped: list[str] = []
    for path in path_order:
        if path in mapped:
            continue
        change_exclusion = next(
            (item for item in policy.ignore_changes if any(repository_paths.matches(pattern, path) for pattern in item.paths)),
            None,
        )
        document_exclusion = next(
            (item for item in policy.ignore_documents if any(repository_paths.matches(pattern, path) for pattern in item.paths)),
            None,
        )
        exclusion = change_exclusion or document_exclusion
        if exclusion is None or path == documentation_policy.CONFIG_PATH:
            unmapped.append(path)
        else:
            kind = "change" if change_exclusion is not None else "document"
            exclusions.append(ScopeExclusion(path, kind, exclusion.reason))
    return DocumentationScope(
        base_sha, comparison_sha, head_sha, "scoped", True, policy_hash,
        proposal_status, proposal_detail, changed_files, tuple(areas), tuple(documents),
        tuple(exclusions), tuple(unmapped), (),
    )


def preview_repository_scope(root: Path, *, base: str, head: str) -> DocumentationScope:
    """Read exact committed objects and return a dirty-tree-independent scope preview."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise DocumentationScopeError("repository_root_invalid") from exc
    if not resolved_root.is_dir():
        raise DocumentationScopeError("repository_root_invalid")
    base_sha = _resolve(resolved_root, _safe_ref(base, field="base"))
    head_sha = _resolve(resolved_root, _safe_ref(head, field="head"))
    merge_output = _run_git(resolved_root, ("merge-base", base_sha, head_sha), allow_failure=True)
    comparison_sha = merge_output.decode("ascii").strip() if merge_output else None
    if comparison_sha is None:
        return DocumentationScope(
            base_sha, None, head_sha, "unavailable", False, None, "unavailable", None,
            (), (), (), (), (), ("comparison_base_unavailable",),
        )
    changed = _changed_files(resolved_root, comparison_sha, head_sha)
    try:
        base_content = _blob(
            resolved_root, base_sha, documentation_policy.CONFIG_PATH,
            maximum=documentation_policy.MAX_CONFIG_BYTES,
        )
    except DocumentationScopeError as exc:
        if exc.code not in _POLICY_OBJECT_ERROR_CODES:
            raise
        return _invalid_base_scope(
            base_sha=base_sha, comparison_sha=comparison_sha, head_sha=head_sha,
            changed_files=changed, policy_hash=None,
            detail=exc.detail or f"accepted policy object is invalid: {exc.code}",
        )
    proposal_status = "unchanged"
    proposal_detail: str | None = None
    try:
        head_content = _blob(
            resolved_root, head_sha, documentation_policy.CONFIG_PATH,
            maximum=documentation_policy.MAX_CONFIG_BYTES,
        )
    except DocumentationScopeError as exc:
        if exc.code not in _POLICY_OBJECT_ERROR_CODES:
            raise
        head_content = None
        proposal_status = "invalid"
        proposal_detail = exc.detail or f"proposal policy object is invalid: {exc.code}"
    if proposal_status != "invalid" and head_content != base_content:
        if head_content is None:
            proposal_status = "removed"
        else:
            try:
                _decode_policy(head_content, role="proposal")
                proposal_status = "valid"
            except DocumentationScopeError as exc:
                proposal_status = "invalid"
                proposal_detail = exc.detail
    if base_content is None:
        if head_content is None or proposal_status == "invalid":
            return plan_scope(
                base_sha=base_sha, comparison_sha=comparison_sha, head_sha=head_sha,
                changed_files=changed, policy=None, policy_hash=None,
                proposal_status=proposal_status, proposal_detail=proposal_detail,
            )
        proposed = _decode_policy(head_content, role="proposal")
        preview = plan_scope(
            base_sha=base_sha, comparison_sha=comparison_sha, head_sha=head_sha,
            changed_files=changed, policy=proposed,
            policy_hash=f"sha256:{hashlib.sha256(head_content).hexdigest()}",
            proposal_status="valid", proposal_detail=None,
        )
        return DocumentationScope(
            preview.base_sha, preview.comparison_sha, preview.head_sha, "not_configured",
            False, preview.policy_hash, preview.proposal_status, preview.proposal_detail,
            preview.changed_files, preview.areas, preview.documents, preview.exclusions,
            preview.unmapped_paths, preview.incomplete_reasons,
        )
    try:
        policy = _decode_policy(base_content, role="base")
    except DocumentationScopeError as exc:
        return _invalid_base_scope(
            base_sha=base_sha, comparison_sha=comparison_sha, head_sha=head_sha,
            changed_files=changed,
            policy_hash=f"sha256:{hashlib.sha256(base_content).hexdigest()}",
            detail=exc.detail or "accepted documentation policy is invalid",
            proposal_status=proposal_status, proposal_detail=proposal_detail,
        )
    for area in policy.areas:
        for document in area.documents:
            try:
                document_content = _blob(
                    resolved_root, base_sha, document, maximum=MAX_GIT_OUTPUT_BYTES
                )
            except DocumentationScopeError as exc:
                if exc.code not in _POLICY_OBJECT_ERROR_CODES:
                    raise
                return _invalid_base_scope(
                    base_sha=base_sha, comparison_sha=comparison_sha,
                    head_sha=head_sha, changed_files=changed,
                    policy_hash=f"sha256:{hashlib.sha256(base_content).hexdigest()}",
                    detail=exc.detail or f"accepted document is invalid: {document}",
                    proposal_status=proposal_status, proposal_detail=proposal_detail,
                )
            if document_content is None:
                return _invalid_base_scope(
                    base_sha=base_sha, comparison_sha=comparison_sha,
                    head_sha=head_sha, changed_files=changed,
                    policy_hash=f"sha256:{hashlib.sha256(base_content).hexdigest()}",
                    detail=f"accepted document is missing: {document}",
                    proposal_status=proposal_status, proposal_detail=proposal_detail,
                )
    return plan_scope(
        base_sha=base_sha, comparison_sha=comparison_sha, head_sha=head_sha,
        changed_files=changed, policy=policy,
        policy_hash=f"sha256:{hashlib.sha256(base_content).hexdigest()}",
        proposal_status=proposal_status, proposal_detail=proposal_detail,
    )


__all__ = [
    "ChangedPath", "DocumentationScope", "DocumentationScopeError", "ScopeExclusion",
    "SelectedArea", "plan_scope", "preview_repository_scope",
]
