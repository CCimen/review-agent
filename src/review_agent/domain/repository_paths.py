"""Validated repository paths and the constrained repository glob language."""

from __future__ import annotations

from fnmatch import fnmatchcase


MAX_REPOSITORY_PATH_CHARS = 500


class RepositoryPathError(ValueError):
    """A path or glob is outside the repository-owned data contract."""


def normalized_path(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise RepositoryPathError(f"{field} must be text")
    path = value.strip()
    if not path or "\x00" in path or len(path) > MAX_REPOSITORY_PATH_CHARS:
        raise RepositoryPathError(
            f"{field} must contain 1 to {MAX_REPOSITORY_PATH_CHARS} characters"
        )
    if "\\" in path:
        raise RepositoryPathError(f"{field} must use forward slashes")
    if path.startswith("/") or any(
        part in {"", ".", ".."} for part in path.split("/")
    ):
        raise RepositoryPathError(f"{field} must be a normalized repository path")
    return path


def simple_glob(value: object, *, field: str) -> str:
    pattern = normalized_path(value, field=field)
    if any(character in pattern for character in "[]{}"):
        raise RepositoryPathError(
            f"{field} supports only simple glob syntax: *, ?, and ** path segments"
        )
    segments = pattern.split("/")
    for position, segment in enumerate(segments):
        if "**" in segment and segment != "**":
            raise RepositoryPathError(
                f"{field} may use ** only as a complete path segment"
            )
        if position and segment == "**" and segments[position - 1] == "**":
            raise RepositoryPathError(
                f"{field} must not contain consecutive ** segments"
            )
    return pattern


def matches(pattern: str, path: str) -> bool:
    """Match validated patterns without recursive or backtracking execution."""
    path_segments = path.split("/")
    reachable = {0}
    for pattern_segment in pattern.split("/"):
        if pattern_segment == "**":
            reachable = set(range(min(reachable), len(path_segments) + 1))
            continue
        reachable = {
            position + 1
            for position in reachable
            if position < len(path_segments)
            and fnmatchcase(path_segments[position], pattern_segment)
        }
        if not reachable:
            return False
    return len(path_segments) in reachable


__all__ = [
    "MAX_REPOSITORY_PATH_CHARS",
    "RepositoryPathError",
    "matches",
    "normalized_path",
    "simple_glob",
]
