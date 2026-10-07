"""Bounded diff snapshots for one tool process, never a source of authority.

Callers validate current access, lease, and PR revisions before every lookup and
again after a fill. Restarting the process or changing the lease simply reloads
the source. Coverage and review completion remain owned by the application.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import sys
from threading import Lock
from typing import Literal

from .changed_files import IndexState
from .diff_render import PreparedDiff

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class DiffSnapshotKey:
    repository: str
    pr_number: int
    run_id: int
    job_id: int
    lease_generation: int
    base_sha: str
    head_sha: str
    reported_files: int


@dataclass(frozen=True, slots=True)
class DiffSnapshot:
    # None remembers a definitive 406 or capped whole-PR rendering. Transport
    # failures are never stored. Missing rendered paths still use patch fallback.
    diff: PreparedDiff | None
    index_state: IndexState = "incomplete"


class ReviewDiffCache:
    """An LRU with limits on retained Python storage and snapshot count."""

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, max_entries: int = 16) -> None:
        if max_bytes < 1 or max_entries < 1:
            raise ValueError("diff cache limits must be positive")
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._entries: OrderedDict[
            tuple[DiffSnapshotKey, Literal["rendered", "patches"]], tuple[DiffSnapshot, int]
        ] = OrderedDict()
        self._bytes = 0
        self._lock = Lock()

    def get(self, key: DiffSnapshotKey, kind: Literal["rendered", "patches"]) -> DiffSnapshot | None:
        with self._lock:
            cache_key = (key, kind)
            entry = self._entries.get(cache_key)
            logger.debug(
                "Diff snapshot lookup run=%s lease=%s kind=%s hit=%s",
                key.run_id, key.lease_generation, kind, entry is not None,
            )
            if entry is None:
                return None
            self._entries.move_to_end(cache_key)
            return entry[0]

    def put(self, key: DiffSnapshotKey, kind: Literal["rendered", "patches"], value: DiffSnapshot) -> None:
        # Include key strings, records, and an allowance for the LRU node/tuple.
        charge = (
            1024 + sys.getsizeof(key) + sys.getsizeof(value)
            + sum(sys.getsizeof(s) for s in (key.repository, key.base_sha, key.head_sha))
            + (value.diff.retained_bytes if value.diff is not None else 0)
        )
        if charge > self._max_bytes:
            logger.debug("Diff snapshot exceeds cache budget kind=%s bytes=%s", kind, charge)
            return
        with self._lock:
            cache_key = (key, kind)
            previous = self._entries.pop(cache_key, None)
            if previous is not None:
                self._bytes -= previous[1]
            while self._entries and (
                len(self._entries) >= self._max_entries or self._bytes + charge > self._max_bytes
            ):
                _, (_, removed_bytes) = self._entries.popitem(last=False)
                self._bytes -= removed_bytes
            self._entries[cache_key] = (value, charge)
            self._bytes += charge
