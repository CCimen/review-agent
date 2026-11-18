"""Closed configuration contract for repository-owned review guidance."""

from __future__ import annotations

from dataclasses import dataclass
import tomllib
from typing import cast


CONFIG_PATH = ".review-agent/config.toml"
INSTRUCTIONS_PATH = ".review-agent/instructions.md"
MAX_CONFIG_BYTES = 16 * 1024
# Ten ordered files cover the intended platform, framework, UI, and repository
# layers while bounding exact-base GitHub reads. The shared text-page capacity
# remains the authoritative limit on combined model context.
MAX_CONTEXT_FILES = 10
_MAX_PATH_CHARS = 500


class RepositoryGuidanceError(ValueError):
    """Repository guidance does not match the supported version-one contract."""


@dataclass(frozen=True, slots=True)
class RepositoryGuidanceConfig:
    enabled: bool
    context_paths: tuple[str, ...]


def _context_path(value: object, *, position: int) -> str:
    if not isinstance(value, str):
        raise RepositoryGuidanceError(
            f"context item {position} must be a Markdown path under context/"
        )
    path = value.strip()
    if (
        not path
        or len(path) > _MAX_PATH_CHARS
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not path.startswith("context/")
    ):
        raise RepositoryGuidanceError(
            f"context item {position} must be a normalized path under context/"
        )
    if not path.endswith(".md"):
        raise RepositoryGuidanceError(
            f"context item {position} must reference a Markdown (.md) file"
        )
    return f".review-agent/{path}"


def parse_config(content: str) -> RepositoryGuidanceConfig:
    """Parse the one explicit index of optional repository guidance files."""
    if len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise RepositoryGuidanceError("config.toml may contain at most 16 KiB")
    try:
        value = cast(dict[str, object], tomllib.loads(content))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise RepositoryGuidanceError("config.toml is not valid TOML") from exc
    allowed = {"version", "enabled", "context"}
    if "version" not in value or not set(value).issubset(allowed):
        raise RepositoryGuidanceError(
            "config.toml fields may only be version, enabled, and context"
        )
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise RepositoryGuidanceError("config.toml version must be 1")
    enabled_value = value.get("enabled", True)
    if not isinstance(enabled_value, bool):
        raise RepositoryGuidanceError("enabled must be a boolean")
    context_value = value.get("context", [])
    if not isinstance(context_value, list):
        raise RepositoryGuidanceError("context must be an array of Markdown paths")
    raw_paths = cast(list[object], context_value)
    if len(raw_paths) > MAX_CONTEXT_FILES:
        raise RepositoryGuidanceError(
            f"context may list at most {MAX_CONTEXT_FILES} files"
        )
    paths = tuple(
        _context_path(item, position=position)
        for position, item in enumerate(raw_paths, start=1)
    )
    if len(set(paths)) != len(paths):
        raise RepositoryGuidanceError("context paths must not contain duplicates")
    return RepositoryGuidanceConfig(
        enabled=enabled_value,
        context_paths=paths,
    )


__all__ = [
    "CONFIG_PATH",
    "INSTRUCTIONS_PATH",
    "MAX_CONFIG_BYTES",
    "MAX_CONTEXT_FILES",
    "RepositoryGuidanceConfig",
    "RepositoryGuidanceError",
    "parse_config",
]
