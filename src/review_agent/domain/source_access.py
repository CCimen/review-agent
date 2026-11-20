"""Credentials for the source operations delegated to a single review lease."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import re
from typing import Literal, TypeGuard
from uuid import UUID


SourceAudience = Literal["review", "graph", "embeddings"]


def valid_proof(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def embedding_proof(graph_proof: str) -> str:
    """Give the parser just the embedding portion of the graph's authority."""
    return hmac.digest(bytes.fromhex(graph_proof), b"review-agent:embeddings:v1", "sha256").hex()


@dataclass(frozen=True, slots=True)
class ReviewSourceAccess:
    review: str = field(repr=False)
    graph: str = field(repr=False)

    @classmethod
    def for_lease(cls, nonce: UUID, *, run_id: int, job_id: int, generation: int) -> ReviewSourceAccess:
        binding = f"review-agent:source:v1:{run_id}:{job_id}:{generation}:".encode("ascii")
        return cls(
            review=hmac.digest(nonce.bytes, binding + b"review", "sha256").hex(),
            graph=hmac.digest(nonce.bytes, binding + b"graph", "sha256").hex(),
        )

    def permits(self, proof: str, audience: SourceAudience) -> bool:
        if not valid_proof(proof):
            return False
        if audience == "embeddings":
            expected = embedding_proof(self.graph)
        else:
            expected = self.graph if audience == "graph" else self.review
        return hmac.compare_digest(expected, proof)
