"""The scope a documentation review covers, as planned from one base and head."""

from __future__ import annotations

from dataclasses import dataclass


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
