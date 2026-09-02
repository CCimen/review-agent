"""Validate one repository-owned review package without network or database access."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from . import capacity, repository_decision_context, repository_guidance_context
from .domain import repository_decisions, repository_guidance


class RepositoryContextValidationError(ValueError):
    """A local repository package cannot be loaded through the runtime contract."""

    code: str
    detail: str | None
    path: str

    def __init__(
        self,
        code: str,
        *,
        path: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:300] if detail else None
        self.path = path


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    path: str
    content_hash: str

    def to_json_obj(self) -> dict[str, str]:
        return {"content_hash": self.content_hash, "path": self.path}


@dataclass(frozen=True, slots=True)
class ValidatedDecision:
    id: str
    path: str
    metadata_hash: str

    def to_json_obj(self) -> dict[str, str]:
        return {
            "id": self.id,
            "metadata_hash": self.metadata_hash,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class RepositoryContextValidationReceipt:
    configured: bool
    enabled: bool
    config_hash: str | None
    instructions: ValidatedFile | None
    context_files: tuple[ValidatedFile, ...]
    decision_index_hash: str | None
    decisions: tuple[ValidatedDecision, ...]
    guidance_chars: int

    def to_json_obj(self) -> dict[str, object]:
        return {
            "config_hash": self.config_hash,
            "configured": self.configured,
            "context_files": [item.to_json_obj() for item in self.context_files],
            "decision_index_hash": self.decision_index_hash,
            "decisions": [item.to_json_obj() for item in self.decisions],
            "enabled": self.enabled,
            "guidance_chars": self.guidance_chars,
            "instructions": (
                self.instructions.to_json_obj()
                if self.instructions is not None
                else None
            ),
            "ready": True,
            "schema_version": 1,
        }


def _hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _target(root: Path, relative_path: str) -> Path:
    target = root / relative_path
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise RepositoryContextValidationError(
            "repository_context_file_missing", path=relative_path
        ) from exc
    try:
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RepositoryContextValidationError(
            "repository_context_path_escape", path=relative_path
        ) from exc
    if target.is_symlink() or not target.is_file():
        raise RepositoryContextValidationError(
            "repository_context_file_invalid", path=relative_path
        )
    return target


def _read(
    root: Path,
    relative_path: str,
    *,
    max_bytes: int,
    max_chars: int | None = None,
    max_lines: int | None = None,
    prefix_only: bool = False,
) -> str:
    target = _target(root, relative_path)
    try:
        with target.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError as exc:
        raise RepositoryContextValidationError(
            "repository_context_file_unavailable", path=relative_path
        ) from exc
    if len(content) > max_bytes and not prefix_only:
        raise RepositoryContextValidationError(
            "repository_context_file_too_large", path=relative_path
        )
    bounded = content[:max_bytes]
    if prefix_only and max_lines is not None:
        bounded = b"".join(bounded.splitlines(keepends=True)[:max_lines])
    try:
        decoded = bounded.decode("utf-8")
    except UnicodeDecodeError as exc:
        if (
            prefix_only
            and exc.end == len(bounded)
            and exc.reason == "unexpected end of data"
        ):
            decoded = bounded[: exc.start].decode("utf-8")
        else:
            raise RepositoryContextValidationError(
                "repository_context_file_not_utf8", path=relative_path
            ) from exc
    lines = decoded.splitlines()
    if max_chars is not None and len(decoded) > max_chars:
        raise RepositoryContextValidationError(
            "repository_context_file_too_large", path=relative_path
        )
    if max_lines is not None and len(lines) > max_lines:
        raise RepositoryContextValidationError(
            "repository_context_file_too_large", path=relative_path
        )
    return "\n".join(lines)


def _optional(root: Path, relative_path: str) -> bool:
    target = root / relative_path
    return target.exists() or target.is_symlink()


def validate_repository_context(
    root: Path,
    *,
    content_max_chars: int | None = None,
) -> RepositoryContextValidationReceipt:
    """Return a bounded receipt for the same explicit files used by review runtime."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise RepositoryContextValidationError(
            "repository_context_root_invalid", path="."
        ) from exc
    if not resolved_root.is_dir():
        raise RepositoryContextValidationError(
            "repository_context_root_invalid", path="."
        )
    requested_maximum = (
        capacity.current().text_page_max_chars
        if content_max_chars is None
        else content_max_chars
    )
    if requested_maximum < 1:
        raise RepositoryContextValidationError(
            "repository_context_capacity_invalid", path=repository_guidance.CONFIG_PATH
        )
    maximum = min(
        requested_maximum,
        repository_guidance_context.MAX_GUIDANCE_CONTENT_CHARS,
    )

    config_hash: str | None = None
    instructions: ValidatedFile | None = None
    context_files: list[ValidatedFile] = []
    guidance_chars = 0
    config_exists = _optional(resolved_root, repository_guidance.CONFIG_PATH)
    enabled = False
    if config_exists:
        try:
            config_content = _read(
                resolved_root,
                repository_guidance.CONFIG_PATH,
                max_bytes=repository_guidance.MAX_CONFIG_BYTES,
                max_lines=repository_guidance_context.MAX_CONFIG_LINES,
            )
            config = repository_guidance.parse_config(config_content)
        except repository_guidance.RepositoryGuidanceError as exc:
            raise RepositoryContextValidationError(
                "repository_guidance_config_invalid",
                path=repository_guidance.CONFIG_PATH,
                detail=str(exc),
            ) from exc
        config_hash = _hash(config_content)
        enabled = config.enabled
        if enabled:
            if _optional(resolved_root, repository_guidance.INSTRUCTIONS_PATH):
                instructions_content = _read(
                    resolved_root,
                    repository_guidance.INSTRUCTIONS_PATH,
                    max_bytes=maximum * 4,
                    max_chars=maximum,
                    max_lines=repository_guidance_context.MAX_GUIDANCE_FILE_LINES,
                )
                try:
                    file = repository_guidance_context.guidance_file(
                        repository_guidance.INSTRUCTIONS_PATH,
                        instructions_content,
                    )
                except repository_guidance_context.RepositoryGuidanceContextError as exc:
                    raise RepositoryContextValidationError(
                        "repository_guidance_instructions_invalid",
                        path=repository_guidance.INSTRUCTIONS_PATH,
                        detail=str(exc),
                    ) from exc
                instructions = ValidatedFile(file.path, file.content_hash)
                guidance_chars += len(file.content)
            for path in config.context_paths:
                content = _read(
                    resolved_root,
                    path,
                    max_bytes=maximum * 4,
                    max_chars=maximum,
                    max_lines=repository_guidance_context.MAX_GUIDANCE_FILE_LINES,
                )
                try:
                    file = repository_guidance_context.guidance_file(path, content)
                except repository_guidance_context.RepositoryGuidanceContextError as exc:
                    raise RepositoryContextValidationError(
                        "repository_guidance_context_invalid",
                        path=path,
                        detail=str(exc),
                    ) from exc
                guidance_chars += len(file.content)
                if guidance_chars > maximum:
                    raise RepositoryContextValidationError(
                        "repository_guidance_capacity_exceeded", path=path
                    )
                context_files.append(ValidatedFile(file.path, file.content_hash))

    decision_index_hash: str | None = None
    decisions: list[ValidatedDecision] = []
    if _optional(resolved_root, repository_decision_context.INDEX_PATH):
        index_content = _read(
            resolved_root,
            repository_decision_context.INDEX_PATH,
            max_bytes=repository_decisions.MAX_INDEX_BYTES,
            max_lines=repository_decisions.MAX_INDEX_LINES,
        )
        try:
            index = repository_decisions.parse_index(index_content)
        except repository_decisions.RepositoryDecisionError as exc:
            raise RepositoryContextValidationError(
                "repository_decision_index_invalid",
                path=repository_decision_context.INDEX_PATH,
                detail=str(exc),
            ) from exc
        decision_index_hash = _hash(index_content)
        for entry in index.entries:
            content = _read(
                resolved_root,
                entry.adr_path,
                max_bytes=capacity.DEFAULT_RESULT_MAX_CHARS,
                max_lines=repository_decisions.MAX_FRONTMATTER_LINES,
                prefix_only=True,
            )
            try:
                decision = repository_decisions.parse_adr(
                    content,
                    match=repository_decisions.DecisionIndexMatch(
                        entry=entry,
                        matched_path_count=1,
                    ),
                )
            except repository_decisions.RepositoryDecisionError as exc:
                raise RepositoryContextValidationError(
                    "repository_decision_invalid",
                    path=entry.adr_path,
                    detail=str(exc),
                ) from exc
            decisions.append(
                ValidatedDecision(
                    id=decision.id,
                    path=decision.adr_path,
                    metadata_hash=decision.metadata_hash,
                )
            )

    return RepositoryContextValidationReceipt(
        configured=config_exists,
        enabled=enabled,
        config_hash=config_hash,
        instructions=instructions,
        context_files=tuple(context_files),
        decision_index_hash=decision_index_hash,
        decisions=tuple(decisions),
        guidance_chars=guidance_chars,
    )


__all__ = [
    "RepositoryContextValidationError",
    "RepositoryContextValidationReceipt",
    "ValidatedDecision",
    "ValidatedFile",
    "validate_repository_context",
]
