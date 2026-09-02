"""Read one bounded repository file from an exact review base snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Protocol

from . import capacity, schemas


_NUMBERED_LINE_RE = re.compile(r"^[0-9]+: ?(.*)$")


class BaseFilePage(Protocol):
    @property
    def state(self) -> str: ...

    @property
    def repository(self) -> str: ...

    @property
    def revision(self) -> str: ...

    @property
    def start_line(self) -> int: ...

    @property
    def total_lines(self) -> int: ...

    @property
    def content(self) -> str: ...

    @property
    def complete_lines(self) -> int: ...

    @property
    def partial_line(self) -> bool: ...


class BaseFileClient(Protocol):
    def get_review_file_page(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
        path: str,
        side: Literal["base"],
        start_line: int,
        max_lines: int,
        max_chars: int,
    ) -> BaseFilePage: ...


class BaseFileLease(Protocol):
    @property
    def job_id(self) -> int: ...

    @property
    def lease_generation(self) -> int: ...


class BaseFileSource(Protocol):
    @property
    def run_id(self) -> int: ...

    @property
    def lease(self) -> BaseFileLease: ...

    @property
    def client(self) -> BaseFileClient: ...


@dataclass(frozen=True, slots=True)
class BaseFileResult:
    state: str
    content: str


def read_base_file(
    source: BaseFileSource,
    *,
    repository: str,
    base_sha: str,
    path: str,
    max_lines: int,
    max_chars: int,
    allow_trailing_lines: bool = False,
) -> BaseFileResult:
    """Read and verify one text file without accepting a partial result."""
    start_line = 1
    lines: list[str] = []
    while start_line <= max_lines:
        page = source.client.get_review_file_page(
            run_id=source.run_id,
            job_id=source.lease.job_id,
            lease_generation=source.lease.lease_generation,
            path=path,
            side="base",
            start_line=start_line,
            max_lines=min(schemas.SOURCE_PAGE_MAX_LINES, max_lines - start_line + 1),
            max_chars=max(
                capacity.MIN_TEXT_PAGE_CHARS,
                min(max_chars, capacity.DEFAULT_RESULT_MAX_CHARS),
            ),
        )
        if (
            page.repository.casefold() != repository.casefold()
            or page.revision != base_sha
            or page.start_line != start_line
        ):
            return BaseFileResult(state="subject_mismatch", content="")
        if page.state != "ok":
            return BaseFileResult(state=page.state, content="")
        if (
            (page.total_lines > max_lines and not allow_trailing_lines)
            or page.complete_lines < 1
        ):
            return BaseFileResult(state="too_large", content="")
        page_lines = page.content.splitlines()
        if len(page_lines) < page.complete_lines or (
            not page.partial_line and len(page_lines) != page.complete_lines
        ):
            return BaseFileResult(state="invalid_page", content="")
        if page.partial_line and not allow_trailing_lines:
            return BaseFileResult(state="too_large", content="")
        for numbered in page_lines[: page.complete_lines]:
            match = _NUMBERED_LINE_RE.fullmatch(numbered)
            if match is None:
                return BaseFileResult(state="invalid_page", content="")
            lines.append(match.group(1))
        content = "\n".join(lines)
        if len(content) > max_chars:
            return BaseFileResult(state="too_large", content="")
        start_line += page.complete_lines
        if page.partial_line and allow_trailing_lines:
            return BaseFileResult(state="ok", content=content)
        if start_line > page.total_lines:
            return BaseFileResult(state="ok", content=content)
        if start_line > max_lines and allow_trailing_lines:
            return BaseFileResult(state="ok", content=content)
    return BaseFileResult(state="too_large", content="")


__all__ = [
    "BaseFileClient",
    "BaseFileLease",
    "BaseFilePage",
    "BaseFileResult",
    "BaseFileSource",
    "read_base_file",
]
