"""Closed identities and bounds for optional, disposable code graph context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import Literal, cast

ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024
SNAPSHOT_MAX_FILES = 10_000
SOURCE_FILE_MAX_BYTES = 2_000_000
GRAPH_MAX_BYTES = 2 * 1024 * 1024 * 1024
GRAPH_MAX_NODES = 100_000
GRAPH_RESULT_MAX_BYTES = 24_000
GRAPH_QUERY_PATH = "/v1/code-graph/query"
GRAPH_EMBEDDINGS_PATH = "/v1/code-graph/embeddings"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_MAX_TEXTS = 64
EMBEDDING_MAX_TEXT_CHARS = 8_192
EMBEDDING_MAX_RESPONSE_BYTES = 4_000_000
INDEXER_REVISION = "crg-2.3.8-review-agent-1"
QUERY_PATTERNS = frozenset({
    "symbol", "semantic", "callers_of", "callees_of", "references_to", "tests_for",
})
EmbeddingMode = Literal["none", "openai"]


class GraphError(ValueError):
    """Optional graph context cannot be obtained within its contract."""


@dataclass(frozen=True, slots=True)
class GraphIdentity:
    run_id: int
    job_id: int
    lease_generation: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> GraphIdentity:
        if set(values) != {"run_id", "job_id", "lease_generation"}:
            raise GraphError("invalid graph lease fields")
        if any(type(value) is not int or value < 1 for value in values.values()):
            raise GraphError("invalid graph lease identity")
        return cls(**cast(dict[str, int], dict(values)))

    def to_mapping(self) -> dict[str, object]:
        return {"run_id": self.run_id, "job_id": self.job_id,
                "lease_generation": self.lease_generation}


@dataclass(frozen=True, slots=True)
class GraphSubject:
    repository_id: int
    repository: str
    head_sha: str
    enabled: bool
    embeddings: EmbeddingMode

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> GraphSubject:
        if set(values) != {"repository_id", "repository", "head_sha", "enabled", "embeddings"}:
            raise GraphError("invalid graph subject fields")
        repository_id, repository, head_sha, enabled, embeddings = (
            values[key] for key in ("repository_id", "repository", "head_sha", "enabled", "embeddings")
        )
        if (type(repository_id) is not int or repository_id < 1
                or not isinstance(repository, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
                or not isinstance(head_sha, str)
                or re.fullmatch(r"[0-9a-f]{40,64}", head_sha) is None
                or type(enabled) is not bool or not isinstance(embeddings, str)
                or embeddings not in {"none", "openai"}):
            raise GraphError("invalid graph subject")
        return cls(repository_id, repository, head_sha, enabled, cast(EmbeddingMode, embeddings))

    @property
    def cache_identity(self) -> str:
        return f"{INDEXER_REVISION}-{self.embeddings}-{EMBEDDING_DIMENSIONS}"

    def to_mapping(self) -> dict[str, object]:
        return {"repository_id": self.repository_id, "repository": self.repository,
                "head_sha": self.head_sha, "enabled": self.enabled, "embeddings": self.embeddings}


@dataclass(frozen=True, slots=True)
class GraphPolicy:
    enabled: bool = False
    embeddings: EmbeddingMode = "none"
    openai_api_key: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> GraphPolicy:
        enabled = environment.get("REVIEW_AGENT_CODE_GRAPH_ENABLED", "false").strip().lower()
        if enabled not in {"true", "false"}:
            raise GraphError("code graph enabled must be true or false")
        if enabled == "false":
            return cls()
        mode = environment.get("REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS", "none").strip()
        if mode not in {"none", "openai"}:
            raise GraphError("code graph embeddings must be none or openai")
        key = environment.get("REVIEW_AGENT_OPENAI_API_KEY", "").strip()
        if mode == "openai" and not key:
            raise GraphError("OpenAI embeddings require REVIEW_AGENT_OPENAI_API_KEY")
        return cls(True, cast(EmbeddingMode, mode), key if mode == "openai" else "")
