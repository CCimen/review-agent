"""Offline dry-run orchestration for reviewer-coach proposal artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import review_agent_coach
import review_agent_coach_proposals
from review_agent_private_io import write_private_file


@dataclass(frozen=True)
class CoachRunArtifactPaths:
    output_dir: Path
    coach_export: Path
    proposal: Path
    summary: Path

    def to_json_obj(self) -> dict[str, str]:
        return {
            "coach_export": str(self.coach_export),
            "proposal": str(self.proposal),
            "summary": str(self.summary),
        }


@dataclass(frozen=True)
class CoachRunArtifacts:
    bundle: review_agent_coach_proposals.ProposalBundle
    paths: CoachRunArtifactPaths


def build_coach_run_artifacts(
    *,
    state: Mapping[str, object],
    output_dir: Path,
    repository: str | None = None,
    after_decision_id: int = 0,
    after_feedback_id: int = 0,
    include_incomplete: bool = False,
    max_candidates: int = review_agent_coach_proposals.DEFAULT_MAX_CANDIDATES,
    min_independent_episodes: int = (
        review_agent_coach_proposals.DEFAULT_MIN_INDEPENDENT_EPISODES
    ),
) -> CoachRunArtifacts:
    coach_payload = review_agent_coach.build_coach_export(
        state,
        repository=repository,
        after_decision_id=after_decision_id,
        after_feedback_id=after_feedback_id,
        include_incomplete=include_incomplete,
    )
    bundle = review_agent_coach_proposals.build_proposal(
        coach_payload,
        max_candidates=max_candidates,
        min_independent_episodes=min_independent_episodes,
    )
    paths = CoachRunArtifactPaths(
        output_dir=output_dir,
        coach_export=output_dir / "coach-export.json",
        proposal=output_dir / "proposal.json",
        summary=output_dir / "SUMMARY.md",
    )
    write_private_file(
        paths.coach_export,
        review_agent_coach.dumps_coach_export(coach_payload),
    )
    write_private_file(
        paths.proposal,
        review_agent_coach_proposals.dumps_proposal_bundle(bundle),
    )
    write_private_file(paths.summary, review_agent_coach_proposals.render_markdown(bundle))
    return CoachRunArtifacts(bundle=bundle, paths=paths)
