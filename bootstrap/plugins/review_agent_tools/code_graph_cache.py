"""Bounded source snapshots and disposable graph artifacts; no application state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path, PurePosixPath
import tarfile

from .code_graph_contract import (
    ARCHIVE_MAX_BYTES,
    GraphError,
    SNAPSHOT_MAX_BYTES,
    SNAPSHOT_MAX_FILES,
    SOURCE_FILE_MAX_BYTES,
)


@dataclass(frozen=True, slots=True)
class SnapshotFiles:
    files: dict[str, str]
    skipped_files: int


def extract_snapshot(
    archive: bytes, root: Path, *, max_bytes: int = SNAPSHOT_MAX_BYTES
) -> SnapshotFiles:
    """Read regular files into a fresh owned directory without tar extraction hooks."""
    if len(archive) > ARCHIVE_MAX_BYTES:
        raise GraphError("archive exceeds the size limit")
    total = 0
    entry_count = 0
    file_count = 0
    skipped = 0
    prefix: str | None = None
    seen: set[str] = set()
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as stream:
            for entry in stream:
                entry_count += 1
                path = PurePosixPath(entry.name)
                if entry_count > SNAPSHOT_MAX_FILES * 2 or entry.size < 0:
                    raise GraphError("archive exceeds the file limit")
                if path.is_absolute() or ".." in path.parts or "\\" in entry.name:
                    raise GraphError("archive contains an unsafe path")
                if not path.parts:
                    raise GraphError("archive contains an empty path")
                if prefix is None:
                    prefix = path.parts[0]
                if path.parts[0] != prefix or (len(path.parts) < 2 and not entry.isdir()):
                    raise GraphError("archive does not have one snapshot root")
                if not (entry.isdir() or entry.isfile() or entry.issym() or entry.islnk()):
                    raise GraphError("archive contains a special file")
                if entry.isdir():
                    continue
                file_count += 1
                if file_count > SNAPSHOT_MAX_FILES:
                    raise GraphError("archive exceeds the file limit")
                relative = PurePosixPath(*path.parts[1:])
                name = relative.as_posix()
                if len(name) > 1024:
                    raise GraphError("archive source path exceeds its bound")
                if name in seen:
                    raise GraphError("archive contains duplicate source paths")
                seen.add(name)
                total += entry.size
                if total > max_bytes:
                    raise GraphError("archive exceeds the expanded size limit")
                if entry.issym() or entry.islnk():
                    skipped += 1
                    continue
                if (entry.size > SOURCE_FILE_MAX_BYTES
                        or any(part in {".git", ".svn", ".code-review-graph"} for part in relative.parts)):
                    skipped += 1
                    continue
                source = stream.extractfile(entry)
                if source is None:
                    raise GraphError("archive file is unavailable")
                with source:
                    content = source.read(SOURCE_FILE_MAX_BYTES + 1)
                if len(content) != entry.size:
                    raise GraphError("archive file size does not match its header")
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                if b"\0" in content:
                    skipped += 1
                    continue
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as destination:
                    destination.write(content)
                files[name] = hashlib.sha256(content).hexdigest()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise GraphError("archive could not be read safely") from exc
    if not files:
        raise GraphError("archive contains no usable source files")
    return SnapshotFiles(files, skipped)
